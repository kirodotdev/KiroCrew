"""docs-lint's fact checks: is this sentence still TRUE of the code?

``path-exists``, ``line-ref``, ``fenced-path``, ``table-row-merge``,
``code-coupled-completeness`` and ``dead-identifier`` each report ``FactFinding``
triples, and the shrink-only baseline in ``.github/docs-lint-baseline.txt`` admits the
ones already recorded. This module holds the six checks, the identifiers, shapes and
scope they share, the four helpers only they use, and the baseline's read, write and
filter. ``scripts/docs_lint.py`` runs the checks, reports them and owns the CLI: it
imports this module and re-exports every name in it, and this module takes the scanned
trees' vocabulary, the finding shapes and the walk helpers from there. That module's
docstring says why the family needs a baseline. Stdlib only, like the gate.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from docs_lint import (
    _FENCE_RE,
    _INLINE_CODE_RE,
    _LINK_RE,
    _SOURCE_CITE_RE,
    CODE_COUPLED_DOCS,
    FactFinding,
    Findings,
    _is_regular_file,
    _prune,
    _read,
    _rel,
    _resolve_source_cite,
    _strip_fences,
)

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
# Inline code with its content captured; ``_INLINE_CODE_RE`` in docs_lint.py blanks
# spans and deliberately captures nothing.
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
# The trailing guard is load-bearing: without it the pattern matches `apps.md`
# inside an i18n key such as `apps.mdNotebook.sort.nameAZ`, which named every
# md-notebook module as a consumer of the packaged `apps.md`. A real filename is
# never followed by another identifier character.
_MD_NAME_RE = re.compile(r"(?<![A-Za-z0-9._-])([A-Za-z0-9][A-Za-z0-9._-]*\.md)(?![A-Za-z0-9])")
# A filename every directory has, so a match cannot be attributed to the packaged
# copy: the hits are a user's project README in the file explorer, not this doc.
_COUPLING_AMBIGUOUS_NAMES: frozenset[str] = frozenset({"README.md"})

# Backticked identifiers, and the trees that decide whether one is alive. Every
# first-party tree is searched, so "dead" means dead repo-wide: restricting the
# corpus to src/, website/src and scripts/ marks `test_all_exports_exact` dead
# (it lives in test/) and `KiroCrewClient` dead (it lives in packages/). The tail
# of the list mirrors `_SOURCE_CITE_TREES`, because a symbol defined only in a
# workflow or in `website/scripts/` is alive and would otherwise read as dead.
# `website/integration/` holds the vitest integration suite, so a doc naming one
# of its test files (`CronNotificationButtons`) cites something real.
# `website/electron/` holds the desktop shell, so a doc naming one of its symbols
# (`checkForUpdates`) cites something real.
_IDENT_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_IDENT_CAMEL_RE = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")
_IDENT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Shorter than this and the token is a word, not a symbol.
_IDENT_MIN_LEN = 8
_IDENT_CODE_TREES: tuple[str, ...] = (
    "src",
    "website/src",
    "website/scripts",
    "website/integration",
    "website/electron",
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


# ── Helpers ────────────────────────────────────────────────────────────────────


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
