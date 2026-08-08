#!/usr/bin/env python3
"""check_brand_name.py — gate the product name's spelling on lines a change adds.

The product is **Kiro Crew**: two words, a space between them, capital ``K``.
Everything else in the tree — the GitHub repo ``kirodotdev/KiroCrew``, the
``kirocrew`` CLI, the ``kiro_crew`` Python package, ``KIROCREW_*`` environment
variables, the ``KiroCrew.dmg`` artifact — is an *identifier*, and identifiers
keep the spelling their system gave them. Only the **prose** name is gated.

## Why diff-scoped and not whole-tree

The tree carries thousands of prose ``KiroCrew``s that predate the convention.
A whole-tree gate would fail every PR until a single enormous rename lands, and
would charge that failure to whoever pushed next. So the enforcing check reads
only the lines the change *adds* (``BRAND_BASE_REF``), which is complete for
regression: a line can only reach ``main`` through a diff that added it. The
whole-tree number is still printed, as a non-failing report, so the backlog
stays visible without ever being anyone's build break.

## Usage

    # enforce on what this branch adds (exit 1 on any violation)
    BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py

    # report the whole-tree backlog, enforce nothing (exit 0)
    python3 scripts/check_brand_name.py

    # scan explicit files, ignoring git entirely
    python3 scripts/check_brand_name.py README.md docs/foo.md

    # self-test: plant one probe per rule family, assert each is caught
    python3 scripts/check_brand_name.py --test

## Escape hatch

A line that must carry the concatenated spelling for a reason the rules below do
not model can opt out with a ``brand-ok`` marker in a trailing comment. Use it
sparingly: it is unscoped and silences the whole line.
"""

from __future__ import annotations

import bisect
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Iterator

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Self-test timing budget for the growth-ratio check. It divides two measured
# durations, so it is only as trustworthy as the smaller one: pair each baseline
# with its own doubled sample and keep the least noisy RATIO, then refuse to
# judge one whose baseline is too small to measure. The floor sits above the
# ~15.625ms granularity Windows reports process CPU time at, so a coarse clock
# cannot on its own manufacture a regression.
_PERF_ATTEMPTS = 5
_PERF_MIN_BASE_SECS = 0.020

# Baseline workloads, tried in order until one produces a measurable baseline.
# A single fixed size cannot serve both ends of the hardware range: 20k costs a
# fast machine ~19-21ms, which straddles the floor above, so the ratio went
# UNJUDGED on a large fraction of runs -- the check reported `ok` while testing
# nothing. Growing the workload buys a measurable baseline instead of abandoning
# the check.
#
# Escalating is close to free because a REGRESSED scan never reaches it: its
# baseline is ~25x the linear one, so it clears the floor at the first size and
# is judged there. Only linear scans -- the fast case -- ever pay for a larger
# size, and a slow runner clears the floor at 20k and pays nothing at all.
_PERF_BASE_SIZES = (20_000, 50_000, 120_000)

# ---------------------------------------------------------------------------
# What counts as a misspelling
# ---------------------------------------------------------------------------

# The concatenated forms. A capital letter inside the token is what makes it a
# rendering of the *brand* rather than of an identifier: `kirocrew` all-lower is
# the CLI command and `KIROCREW` all-upper is the env-var prefix, and neither is
# ever prose, so neither appears here. `(?<![A-Za-z0-9_])` / `(?![A-Za-z0-9_])`
# keep `KiroCrewApps` and `MyKiroCrewShim` out: those are single identifiers.
CONCATENATED = re.compile(
    r"(?<![A-Za-z0-9_])(?:KiroCrew|Kirocrew|kiroCrew|KiroCREW)(?![A-Za-z0-9_])"
)

# Spaced but uncapitalised. What keeps the `~/.kiro/crew` data directory and the
# `kiro-crew-security-support` alias out is the literal **space** — neither can
# match this pattern at all. The leading class narrows something else: a spaced
# form that follows `.`, `/`, `\` or `-`, which is a fragment of some longer
# token rather than the two-word brand.
UNCAPITALISED = re.compile(r"(?<![A-Za-z0-9_./\\-])kiro crew(?![A-Za-z0-9_])")

CORRECT = "Kiro Crew"

# ---------------------------------------------------------------------------
# Structural exemptions — places the concatenated spelling is the true name
# ---------------------------------------------------------------------------

# Shipped artifact filenames: KiroCrew.dmg, KiroCrew.exe, KiroCrew.app ...
# The list is closed on purpose. `KiroCrew.` followed by anything else — most
# importantly by end-of-sentence — stays a violation.
#
# These three are applied with `.match(line, pos)`, which anchors at ``pos``, so
# none of them carries a `^`: slicing the line to give each one its own string
# would copy the whole tail once per match.
ARTIFACT_EXT = re.compile(
    r"\.(?:exe|dmg|app|lnk|zip|AppImage|deb|rpm|pkg|msi|ico|icns|iconset"
    r"|plist|entitlements|desktop|service|blockmap|nupkg|tar|gz|sha256)"
    r"(?![A-Za-z0-9])"
)

# Release-artifact suffixes: KiroCrew-x86_64.AppImage, KiroCrew-notarized-...,
# KiroCrew-Nightly. Deliberately a closed list rather than "hyphen means
# artifact", so prose like `KiroCrew-specific` is still reported.
ARTIFACT_SUFFIX = re.compile(
    r"-(?:x86_64|aarch64|arm64|amd64|x64|universal|notarized|unnotarized"
    r"|[Nn]ightly|[Ss]etup|[Pp]ortable|mac|darwin|linux|win(?:dows)?"
    r"|\d+\.\d+)(?![A-Za-z0-9])"
)

# The channel-qualified product name, which is a **space**-separated OS
# identifier: ``PRODUCT_NAMES`` in ``cli_desktop.py`` spells the macOS log
# directory ``~/Library/Logs/KiroCrew Nightly`` and the Windows config directory
# the same way, and electron's ``instance-guard.js`` keeps ``appName`` distinct
# from its ``displayName`` for exactly this reason. Prose and identifier are the
# same characters here, so no rule can separate them — the identifier wins,
# because renaming it would move a user's logs and config out from under them.
CHANNEL_SUFFIX = re.compile(r" (?:[Nn]ightly|[Ii]nsider|[Ss]table)(?![A-Za-z0-9])")

# Where a URL can begin. The `(?<![A-Za-z0-9.-])` lookbehind is what keeps this
# linear, and not just correct: a host can only START where the preceding character
# is not itself a host character, so a long run of them offers exactly one starting
# position instead of one per character. Without it, `finditer` would re-walk the
# run from every offset and a single long token would cost O(len²).
URL_START = re.compile(
    r"https?://|git@|ssh://|(?<![A-Za-z0-9.-])[A-Za-z0-9][A-Za-z0-9.-]*"
    r"\.(?:com|dev|io|org)/"
)

# Characters that end a URL. Markup and punctuation around a link are not part of
# it, so `<a href="https://example.com/">KiroCrew` must not read as "inside a URL"
# — the brand there is visible prose.
URL_TERMINATOR = re.compile(r"[\"'<>()\[\]`,;\s]")


def hyphen_interior(line: str, start: int) -> bool:
    """Is the brand at ``start`` inside a hyphenated identifier?

    The ``X-KiroCrew-Proxy`` header is the case that matters. Only the hyphen
    itself is required: demanding a word character before it as well would let
    this rule carry an untestable branch, since the tree holds no ``-KiroCrew``
    preceded by anything other than a word character. Prose is unaffected either
    way — ``KiroCrew-owned`` has a *space* before the brand, not a hyphen.

    Takes a position rather than the text before it, because slicing the line to
    produce that text costs O(length) once per match.
    """
    return start > 0 and line[start - 1] == "-"


def url_spans(line: str) -> list[tuple[int, int]]:
    """Half-open ranges the line's URLs cover, in one left-to-right pass.

    Computed per LINE, never per match. Asking "is this position inside a URL?"
    by rescanning the text before each match costs O(matches x length), which a
    line carrying many brand tokens turns into seconds of CPU.

    A URL runs from its start to the first character that cannot be part of one.
    Markup and punctuation therefore end it, which is what keeps
    ``<a href="https://example.com/">KiroCrew`` out of the span: the brand there
    is visible prose, not part of the link.
    """
    spans: list[tuple[int, int]] = []
    for m in URL_START.finditer(line):
        start = m.start()
        if spans and start < spans[-1][1]:
            continue  # already inside the URL we are holding
        stop = URL_TERMINATOR.search(line, m.end())
        spans.append((start, stop.start() if stop else len(line)))
    return spans


def covered(spans: list[tuple[int, int]], starts: list[int], pos: int) -> bool:
    """Is ``pos`` inside one of these non-overlapping, sorted spans?

    Binary search rather than a linear scan, so a line with many spans and many
    matches does not degrade into a quadratic pairing of the two.
    """
    i = bisect.bisect_right(starts, pos) - 1
    return i >= 0 and pos < spans[i][1]


SUPPRESSION = re.compile(r"brand-ok")

# Binary-ish and vendored trees the gate has no business reading. Everything
# else in the repo is in scope — including the shipped locale catalogs, whose
# translated values render the brand to users.
SKIP_DIRS = (
    ".git/",
    "node_modules/",
    "website/dist/",
    "website/node_modules/",
    "site/node_modules/",
    "temp-screenshots/",
    # AGENTS.md excludes _vendor/ from every linter, and it holds native libraries
    # whose suffixes (`.0`, `.dylib`) no extension list will ever fully enumerate.
    "src/kiro_crew/_vendor/",
)
SKIP_SUFFIXES = (
    ".lock",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".icns",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp4",
    ".pdf",
    ".zip",
    ".gz",
)
SKIP_PATHS = (
    "package-lock.json",
    "website/package-lock.json",
    "THIRD-PARTY-NOTICES.md",
    "NOTICE",
    # Every brand-shaped string in here is the packaged `.app` basename, and JSON
    # has no comments, so the per-line escape hatch cannot reach it.
    "website/electron/package.json",
    # This gate's own rule text and probes spell every forbidden form out.
    "scripts/check_brand_name.py",
    "test/test_brand_name_gate.py",
)

# Generated artifacts. Counted by the whole-tree report, never enforced.
#
# The locale catalogs are machine-translated and carry ~85 joined spellings each;
# `tips_catalog.json` is derived from `src/kiro_crew/docs/*.md`. Both are JSON, so
# neither offers a line the `brand-ok` comment could sit on, and both re-emit their
# lines wholesale when regenerated or re-indented — which would fail a PR on text its
# author neither wrote nor can correct where the error points. Their sources stay
# enforced (`en.json`, `en.manual.json`, and the docs), so coverage is unchanged and
# a fix there is what reaches the artifact.
GENERATED_PATHS = (
    re.compile(r"^website/src/i18n/locales/(?!en\.json$|en\.manual\.json$)[\w-]+\.json$"),
    re.compile(r"^src/kiro_crew/data/tips_catalog\.json$"),
)


@dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    token: str
    text: str

    def render(self) -> str:
        head = f"{self.path}:{self.line_no}: {self.token!r} -> {CORRECT!r}"
        return f"{head}\n    {self.text.strip()[:160]}"


# ---------------------------------------------------------------------------
# Markdown code context
# ---------------------------------------------------------------------------


def fenced_lines(lines: list[str]) -> set[int]:
    """1-based line numbers inside a fenced code block.

    Shell snippets are the single largest source of legitimate concatenated
    spellings in the docs (``cd KiroCrew``, ``git clone .../KiroCrew.git``), and
    they are all inside fences. Tracking fence state needs the whole file, which
    is why the scanner reads full blobs and filters to added lines afterwards
    rather than scanning a diff hunk directly.

    Width and character both matter. A fence closes only on a run of the SAME
    character at least as long as the one that opened it, and carrying no info
    string — which is what lets a four-backtick block quote a three-backtick
    example without the inner run ending the outer block and exposing its
    contents as prose.
    """
    inside: set[int] = set()
    fence: tuple[str, int] | None = None
    for i, raw in enumerate(lines, start=1):
        stripped = raw.lstrip()
        m = re.match(r"^(`{3,}|~{3,})", stripped)
        if m:
            run = m.group(1)
            char, width = run[0], len(run)
            if fence is None:
                fence = (char, width)
                inside.add(i)
                continue
            if char == fence[0] and width >= fence[1] and not stripped[width:].strip():
                inside.add(i)
                fence = None
                continue
        if fence is not None:
            inside.add(i)
    return inside


def inline_code_spans(line: str) -> list[tuple[int, int]]:
    """Half-open [start, end) ranges covered by backtick spans.

    Pairs adjacent runs of equal length in one left-to-right pass. Searching
    forward for each run's partner instead would rescan the tail of the line once
    per run, which is quadratic on a line carrying many backticks.
    """
    runs = [(m.start(), m.end() - m.start()) for m in re.finditer(r"`+", line)]
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(runs):
        start, width = runs[i]
        partner = next((j for j in range(i + 1, len(runs)) if runs[j][1] == width), None)
        if partner is None:
            break
        spans.append((start, runs[partner][0] + width))
        i = partner + 1
    return spans


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan_line(path: str, line_no: int, line: str, *, in_code: bool) -> Iterator[Violation]:
    if in_code or SUPPRESSION.search(line):
        return
    is_markdown = path.endswith((".md", ".mdx"))
    # Both of these are per-LINE. Deriving either inside the match loop would
    # rescan the same text once per brand token, which is the quadratic shape a
    # line carrying thousands of them exploits.
    code_spans = inline_code_spans(line) if is_markdown else []
    url_ranges = url_spans(line)
    code_starts = [s for s, _ in code_spans]
    url_starts = [s for s, _ in url_ranges]

    for m in CONCATENATED.finditer(line):
        start, end = m.span()
        if covered(code_spans, code_starts, start):
            continue
        # Index into the line; never slice it. `line[:start]` copies the prefix
        # once per match, which is what made a line of many brand names quadratic.
        if start > 0 and line[start - 1] in "/\\":
            continue  # path segment or repo slug
        if end < len(line) and line[end] in "/\\":
            continue  # path segment, other side
        if hyphen_interior(line, start):
            continue  # X-KiroCrew-Proxy and friends
        if covered(url_ranges, url_starts, start):
            continue  # inside a URL
        if ARTIFACT_EXT.match(line, end) or ARTIFACT_SUFFIX.match(line, end):
            continue  # release artifact filename
        if CHANNEL_SUFFIX.match(line, end):
            continue  # channel-qualified OS identifier
        yield Violation(path, line_no, m.group(), line)

    for m in UNCAPITALISED.finditer(line):
        if covered(code_spans, code_starts, m.start()):
            continue
        yield Violation(path, line_no, m.group(), line)


def read_lines(path: str) -> list[str] | None:
    """The file's lines as *git* models them, or ``None`` if it cannot be read.

    ``newline=""`` disables universal-newline translation and the split is on
    ``\\n`` alone, which is the only line separator git counts. Translating would
    make a lone ``\\r`` start a new line here but not in the diff, and every line
    number after it would point at the wrong text.
    """
    try:
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8", newline="") as fh:
            return fh.read().split("\n")
    except (OSError, UnicodeDecodeError):
        return None


def scan_lines(path: str, lines: list[str], only_lines: set[int] | None = None) -> list[Violation]:
    code_lines = fenced_lines(lines) if path.endswith((".md", ".mdx")) else set()
    found: list[Violation] = []
    for line_no, line in enumerate(lines, start=1):
        if only_lines is not None and line_no not in only_lines:
            continue
        found.extend(scan_line(path, line_no, line, in_code=line_no in code_lines))
    return found


def scan_file(path: str, only_lines: set[int] | None = None) -> list[Violation]:
    """Scan one file, optionally restricted to a set of 1-based line numbers.

    Unreadable files report nothing. That is right for the whole-tree *report*,
    which walks everything git tracks; the enforcing path must not use this, and
    calls :func:`read_lines` itself so it can fail closed instead.
    """
    lines = read_lines(path)
    if lines is None:
        return []
    return scan_lines(path, lines, only_lines)


def in_scope(path: str) -> bool:
    """Is this path readable text the report should count?"""
    if path in SKIP_PATHS:
        return False
    if any(path.startswith(d) for d in SKIP_DIRS):
        return False
    if path.endswith(SKIP_SUFFIXES):
        return False
    return True


def enforced(path: str) -> bool:
    """Is this path one a change can be held responsible for?

    Narrower than :func:`in_scope` by exactly the generated artifacts, so the report
    keeps showing the whole backlog while the gate only blocks on text an author
    actually wrote.
    """
    return in_scope(path) and not any(p.match(path) for p in GENERATED_PATHS)


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------


def git(args: list[str]) -> str:
    """Run git and decode its output tolerantly.

    ``errors="replace"`` matters because ``--text`` makes git emit the *content*
    of a file that is not valid UTF-8, and a strict decode would raise inside
    ``subprocess`` — a traceback instead of a verdict. Everything this function's
    callers parse (hunk headers, NUL-separated paths) is ASCII, so a mangled body
    costs nothing. Strictness belongs in :func:`read_lines`, which is where an
    undecodable file becomes a deliberate fail-closed.
    """
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def tracked_files() -> list[str]:
    return [p for p in git(["ls-files"]).splitlines() if in_scope(p)]


def diff_base(base: str) -> str:
    """The commit to measure against.

    ``merge-base`` is the honest divergence point, but a shallow CI clone fetches
    the base commit as its own tip with no shared history, so it often has none.
    The base tip is then the fallback.
    """
    try:
        return git(["merge-base", base, "HEAD"]).strip()
    except subprocess.CalledProcessError:
        return base


def changed_paths(frm: str) -> list[str]:
    """In-scope paths this change touches.

    ``-z`` is what makes this trustworthy: without it git quotes any path holding
    a non-ASCII or unusual byte (``"b/docs/\\346\\227\\245.md"``), and a parser
    reading ``+++ b/`` lines then silently drops that file — a gate that skips a
    changed file is worse than no gate.
    """
    try:
        out = git(["diff", "--name-only", "-z", "--diff-filter=d", frm])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::brand gate: cannot diff against {frm} — the base commit is not "
            f"present. Fetch it before running, or unset BRAND_BASE_REF to report "
            f"whole-tree counts without enforcing.\n{exc.stderr}"
        )
    return [p for p in out.split("\0") if p and enforced(p)]


def added_lines(frm: str, path: str) -> set[int]:
    """1-based line numbers this change adds to ``path``.

    The diff runs base-to-**working-tree**. CI checks out a clean tree, so that is
    the same as base-to-``HEAD`` there; locally it means the gate sees edits that
    are not committed yet, which is the only form in which a local run is useful.
    A brand-new *untracked* file is the one gap, and it is caught on the commit
    that tracks it.

    ``--text`` forces hunks even for a path that ``.gitattributes`` marks
    ``-diff``: git would otherwise report only "Binary files differ", leaving
    nothing to scan and passing the file silently. ``in_scope`` has already
    dropped the genuinely binary suffixes.
    """
    diff = git(["diff", "--unified=0", "--no-color", "--text", frm, "--", path])
    added: set[int] = set()
    for raw in diff.splitlines():
        if not raw.startswith("@@"):
            continue
        # `@@ -old,count +new,count @@` — the `+` side is the post-image, and a
        # missing count means exactly one line. A pure deletion reports `+n,0`,
        # which correctly contributes nothing.
        m = re.search(r"\+(\d+)(?:,(\d+))?", raw)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        added.update(range(start, start + count))
    return added


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

PROBES: tuple[tuple[str, str, bool], ...] = (
    ("prose", "Run KiroCrew on your laptop.", True),
    ("possessive", "This is KiroCrew's own sandbox.", True),
    ("sentence-end", "shipped with KiroCrew.", True),
    ("uncapitalised", "Install kiro crew first.", True),
    ("cli-command", "Run `kirocrew serve` to start it.", False),
    ("env-var", 'os.environ["KIROCREW_HOME"]', False),
    ("module", "from kiro_crew.config import loader", False),
    ("repo-slug", "https://github.com/kirodotdev/KiroCrew/issues", False),
    ("wrapped-slug", "see [docs](https://github.com/kirodotdev/KiroCrew/issues)", False),
    ("url-query", "https://example.com/d?app=KiroCrew&v=2", False),
    ("markup-after-url", '<a href="https://example.com/">KiroCrew</a>', True),
    ("url-then-prose", "https://example.com/x is where KiroCrew lives", True),
    # One-sided on purpose, all four. A probe with a separator on BOTH sides of the
    # brand is satisfied by either check alone, so it cannot tell which one broke.
    ("path-before", "built from ~/src/KiroCrew last night", False),
    ("path-after", "KiroCrew/website holds the frontend", False),
    ("windows-path-before", r"installed to C:\Program Files\KiroCrew", False),
    ("windows-path-after", r"launches KiroCrew\resources\app.asar", False),
    ("glued-identifier", "resolve KiroCrewApps from the registry", False),
    ("artifact-ext", "signs KiroCrew.exe and Update.exe", False),
    ("artifact-suffix", "publishes KiroCrew-x86_64.AppImage", False),
    ("channel-identifier", 'home / "Library" / "Logs" / "KiroCrew Nightly"', False),
    ("http-header", 'assert "X-KiroCrew-Proxy" in headers', False),
    ("hyphen-prose", "every KiroCrew-owned file", True),
    ("data-dir", "state lives under ~/.kiro/crew/workspace", False),
    ("suppressed", "correct = 'KiroCrew'  # brand-ok: dictionary fixture", False),
)


def self_test() -> int:
    failures = 0
    for label, line, should_flag in PROBES:
        hits = list(scan_line("probe.py", 1, line, in_code=False))
        flagged = bool(hits)
        if flagged != should_flag:
            verb = "was not flagged" if should_flag else f"was flagged ({hits[0].token!r})"
            print(f"  FAIL {label}: {verb} — {line}")
            failures += 1
        else:
            print(f"  ok   {label}")

    # Fenced code in markdown is exempt; the prose around it is not.
    doc = ["Run KiroCrew.", "```bash", "cd KiroCrew", "```", "KiroCrew is done."]
    fenced = fenced_lines(doc)
    if fenced != {2, 3, 4}:
        print(f"  FAIL fence-tracking: expected {{2, 3, 4}}, got {fenced}")
        failures += 1
    else:
        print("  ok   fence-tracking")

    md_hits = [
        v.line_no
        for i, line in enumerate(doc, start=1)
        for v in scan_line("doc.md", i, line, in_code=i in fenced)
    ]
    if md_hits != [1, 5]:
        print(f"  FAIL fence-exemption: expected prose lines [1, 5], got {md_hits}")
        failures += 1
    else:
        print("  ok   fence-exemption")

    inline = list(scan_line("doc.md", 1, "clone `KiroCrew` then run KiroCrew.", in_code=False))
    if len(inline) != 1:
        print(f"  FAIL inline-code: expected 1 hit outside the span, got {len(inline)}")
        failures += 1
    else:
        print("  ok   inline-code")

    # A generated file can carry one very long line. Every check on the path to a
    # verdict has to stay linear in its length, or the job times out on input a
    # contributor cannot see is pathological.
    #
    # Two shapes, because they reach different checks. A filler separated from the
    # brand by a space leaves `token_prefix` empty, so it exercises CONCATENATED,
    # UNCAPITALISED and the backtick scan but never `inside_url`. Only a filler
    # GLUED to the brand hands `URL_START` the whole prefix, which is the one
    # place an unbounded host quantifier would go quadratic.
    start = time.monotonic()
    for filler, glue in ((".", " "), ("a-", " "), ("x.com", " "), ("`", " "), ("a.", "")):
        long_line = filler * (200_000 // len(filler)) + glue + "KiroCrew"
        if not list(scan_line("big.md", 1, long_line, in_code=False)):
            print(f"  FAIL linearity: missed the brand after a {filler!r} run")
            failures += 1
    elapsed = time.monotonic() - start
    if elapsed > 2.0:
        print(f"  FAIL linearity: 5 x 200k-char lines took {elapsed:.1f}s (expected < 2s)")
        failures += 1
    else:
        print(f"  ok   linearity ({elapsed:.2f}s for 5 x 200k chars)")

    # The uncapitalised rule has its own inline-code exemption, on a separate
    # branch from the concatenated one above.
    if list(scan_line("doc.md", 1, "run `kiro crew` from a shell", in_code=False)):
        print("  FAIL inline-code-lower: a spaced form inside a code span was flagged")
        failures += 1
    else:
        print("  ok   inline-code-lower")

    # Many brand names in ONE whitespace-free run. This is the shape that goes
    # quadratic the moment any per-match step slices the line or rescans its
    # prefix. The assertion is on the GROWTH RATIO, not a wall-clock budget: an
    # absolute threshold generous enough for a loaded CI runner is also generous
    # enough to let a quadratic implementation pass at this size.
    #
    # Measuring a ratio puts the whole burden on the timer, and the original
    # form (one `time.monotonic()` sample per size) had two independent ways to
    # report a regression that was not there. It was the single largest source
    # of Windows CI flakes:
    #
    # * WRONG CLOCK. `time.monotonic()` is `GetTickCount64()` on Windows, a
    #   ~15.625ms tick. The base scan costs ~60ms there, so a sample was only
    #   ~4 ticks wide and quantisation ALONE moved the ratio ~25%. Every
    #   observed failure reported times that were exact multiples of 15.625ms
    #   (0.047/0.062/0.109 -> 0.156/0.188/0.203).
    # * WALL CLOCK AT ALL. Four xdist workers on a 4-vCPU runner means the
    #   timed region gets descheduled, and the LONGER scan absorbs more
    #   preemption than the shorter one -- which inflates the ratio
    #   systematically rather than symmetrically. Taking the best of several
    #   wall-clock samples does NOT fix this (measured: it made linear scans
    #   breach 3.0x MORE often, because the shorter scan cleans up better).
    #
    # So measure CPU time, which simply does not advance while the thread is
    # off-CPU, and pair each baseline with its own doubled sample so the two
    # halves of a ratio always come from the same conditions. Measured under 2x
    # CPU oversubscription: a linear scan stays at most 2.02x (never breaching)
    # while a deliberately quadratic one never drops below 3.88x, so this keeps
    # every bit of the check's teeth. `process_time` is also coarse on Windows,
    # so the floor below still applies.
    def ratio_of(base: int) -> tuple[float, float, int, int]:
        """Best (least noisy) doubled/base CPU-time ratio over several attempts."""
        best = math.inf
        best_pair = (0.0, 0.0)
        found: tuple[int, int] = (0, 0)

        def once(count: int) -> tuple[float, int]:
            began = time.process_time()
            hits = len(list(scan_line("big.md", 1, "!KiroCrew" * count, in_code=False)))
            return time.process_time() - began, hits

        for _ in range(_PERF_ATTEMPTS):
            base_time, base_hits = once(base)
            doubled_time, doubled_hits = once(base * 2)
            found = (base_hits, doubled_hits)
            if base_time <= 0.0:
                continue
            candidate = doubled_time / base_time
            if candidate < best:
                best, best_pair = candidate, (base_time, doubled_time)
        return (0.0 if best is math.inf else best), best_pair[0], found[0], found[1]

    # Grow the workload until the baseline is big enough to divide. `ratio_of`
    # is only called again when the previous size came in under the floor, so
    # the common cases cost exactly one call.
    base_count = 0
    ratio = base_time = 0.0
    base_found = doubled_found = 0
    for base_count in _PERF_BASE_SIZES:
        ratio, base_time, base_found, doubled_found = ratio_of(base_count)
        if base_time >= _PERF_MIN_BASE_SECS:
            break

    if (base_found, doubled_found) != (base_count, base_count * 2):
        print(f"  FAIL repeated-brands: found {base_found}/{doubled_found}, "
              f"want {base_count}/{base_count * 2}")
        failures += 1
    elif base_time < _PERF_MIN_BASE_SECS:
        # Even the largest workload was too fast to measure. Quadratic growth at
        # that size costs orders of magnitude more than the floor, so this cannot
        # be hiding a regression -- report the fact rather than dividing noise by
        # noise.
        print(f"  ok   repeated-brands (baseline {base_time * 1000:.1f}ms at {base_count} "
              f"brands still below the {_PERF_MIN_BASE_SECS * 1000:.0f}ms measurement "
              f"floor; ratio not judged)")
    elif ratio > 3.0:
        print(f"  FAIL repeated-brands: doubling the input cost {ratio:.1f}x CPU time "
              f"(best of {_PERF_ATTEMPTS}, baseline {base_time:.3f}s at {base_count} "
              f"brands); linear is ~2x, so a per-match scan of the line has come back")
        failures += 1
    else:
        print(f"  ok   repeated-brands (doubling cost {ratio:.1f}x at {base_count} "
              f"brands, linear)")

    # A wider fence is not closed by a narrower run inside it, so a doc can quote
    # a fenced example without exposing its contents as prose.
    nested = ["````markdown", "```bash", "cd KiroCrew", "```", "````", "Then run KiroCrew."]
    nested_fenced = fenced_lines(nested)
    if nested_fenced != {1, 2, 3, 4, 5}:
        print(f"  FAIL fence-width: expected {{1..5}} inside, got {nested_fenced}")
        failures += 1
    else:
        print("  ok   fence-width")

    print("self-test passed" if not failures else f"self-test FAILED ({failures})")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def report(violations: Iterable[Violation], *, enforcing: bool, base: str | None) -> int:
    violations = list(violations)
    if not violations:
        scope = f"lines added since {base}" if enforcing else "whole tree"
        print(f"brand gate: no misspellings of {CORRECT!r} in the {scope} ✓")
        return 0

    if enforcing:
        print(f"::error::brand gate: {len(violations)} line(s) added by this change "
              f"spell the product name wrong. It is {CORRECT!r} — two words, capital K.")
    else:
        print(f"::notice::brand gate report: {len(violations)} pre-existing line(s) "
              f"spell the product name something other than {CORRECT!r}. Not enforced "
              f"here; only lines a change adds are gated.")
    # The listing is path-sorted, so a silently-truncated report shows only the
    # alphabetically-first paths — '.github/' and '.kiro/' alone exceed the report
    # budget, which is how a backlog of UI-visible strings under 'src/' and
    # 'website/' stayed invisible for a whole rename. Always disclose the cut, and
    # on the report path precede the listing with a per-directory tally so the
    # shape of the backlog survives truncation.
    shown = 200 if enforcing else 40
    if not enforcing and len(violations) > shown:
        tally: dict[str, int] = {}
        for v in violations:
            head, _, tail = v.path.partition("/")
            tally[f"{head}/" if tail else head] = tally.get(f"{head}/" if tail else head, 0) + 1
        print(f"\nby top-level path ({len(tally)} entries, all {len(violations)} lines):")
        for name, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count:>5}  {name}")
        print()

    for v in violations[:shown]:
        print(v.render())
    if len(violations) > shown:
        print(f"... and {len(violations) - shown} more")
    if enforcing:
        print(
            "\nIdentifiers keep their own spelling and are already exempt: the "
            "kirodotdev/KiroCrew slug, KiroCrew.dmg-style artifacts, the KiroCrew Nightly "
            "channel, KIROCREW_* env vars, the kirocrew CLI, kiro_crew imports, and "
            "fenced/inline code in markdown. For anything else that genuinely needs the "
            "joined form, add a 'brand-ok' comment on the line."
            "\n\nOne case wants a reword rather than a substitution: a hyphenated compound. "
            "'KiroCrew-owned' does not become 'Kiro Crew-owned' — hyphenating an open "
            "two-word name reads wrong. Write 'owned by Kiro Crew' instead."
        )
    return 1 if enforcing else 0


def enforce_diff(base: str) -> int:
    """Enforce on the lines this change adds. Fails closed on anything unreadable."""
    frm = diff_base(base)
    found: list[Violation] = []
    unreadable: list[str] = []
    for path in changed_paths(frm):
        lines = added_lines(frm, path)
        if not lines:
            continue
        content = read_lines(path)
        if content is None:
            unreadable.append(path)
            continue
        found.extend(scan_lines(path, content, lines))

    if unreadable:
        # Reporting nothing for a file the change actually touched is how a gate
        # quietly stops gating, so refuse to pass instead of skipping.
        print(
            "::error::brand gate: cannot read these changed files as UTF-8 text, so "
            "the product name in them was never checked. Either make them decodable "
            "or add their suffix to SKIP_SUFFIXES in scripts/check_brand_name.py:"
        )
        for path in unreadable:
            print(f"  {path}")
        return 1

    return report(found, enforcing=True, base=base)


def force_utf8_output() -> None:
    """Print UTF-8 whatever the console's default encoding is.

    Both halves of this gate's output carry non-ASCII: a violation can name a
    non-ASCII path, and the clean verdict ends in a check mark. Windows consoles
    default to cp1252, which raises ``UnicodeEncodeError`` on either — turning a
    PASS into a traceback and failing the build on a tree that was fine.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    force_utf8_output()
    if "--test" in argv:
        return self_test()

    explicit = [a for a in argv if not a.startswith("-")]
    if explicit:
        # Same fail-closed rule as enforce_diff: a path that yields no lines has
        # not been checked, so reporting it clean is a false green. Without this,
        # a typo'd or moved path prints the success line and exits 0.
        unreadable = [p for p in explicit if read_lines(p) is None]
        if unreadable:
            print(
                "::error::brand gate: cannot read these paths as UTF-8 text, so the "
                "product name in them was never checked:"
            )
            for path in unreadable:
                print(f"  {path}")
            return 1
        found = [v for p in explicit for v in scan_file(p)]
        return report(found, enforcing=True, base=None)

    base = os.environ.get("BRAND_BASE_REF", "").strip()
    if base:
        return enforce_diff(base)

    found = [v for path in tracked_files() for v in scan_file(path)]
    return report(found, enforcing=False, base=None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
