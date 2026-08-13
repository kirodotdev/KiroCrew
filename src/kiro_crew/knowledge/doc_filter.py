"""Which files in a project tree are documents worth auto-ingesting.

One rule decides every case:

    Auto-add prose written for HUMANS about intent, decisions, and how things
    work. Exclude prose written for AGENTS, generated files, and
    machine-readable lists.

The rule is needed because ``FileReader.SUPPORTED`` accepts source code, logs,
CSVs and extensionless files, and per-source filtering in
:mod:`kiro_crew.knowledge.folder_watcher` is denylist-only -- there is no
include allowlist. Pointing a folder source at a code repository unfiltered
therefore ingests the repository rather than its documentation, and every chunk
costs one LLM extraction call.

The rule is expressed as the ``properties`` a folder source already understands
(``include_extensions``, ``ignore_patterns``, ``extra_skip_dirs``,
``min_file_bytes``), so the ordinary scan path applies it with no special
casing. :func:`should_ingest_doc` is the same rule as a directly callable
predicate, for tests and for measuring a tree without running a scan; a test
pins the two against each other so they cannot drift.

Measured on the Kiro Crew tree: 2706 walked files, 277 with a document
extension, 165 after the full filter -- 147 of those in the design/spec/
reference corpus. File filters control POLLUTION; only a chunk budget controls
COST (``knowledge.auto_ingest_chunk_budget`` here,
``knowledge.folder_ingest_chunk_budget`` for a folder the user adds by hand),
because a handful of large documents dominate the chunk count.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

from .folder_watcher import DEFAULT_IGNORE_GLOBS, HARD_SKIP_DIRS

#: Extensions that carry human prose. ``.txt`` is deliberately absent: inside a
#: code repository it is nearly always a machine-readable list rather than
#: prose (``SOURCES.txt``, ``scrub-allowlist.txt``,
#: ``windows-expected-failures.txt``), and a list ingests as noise that answers
#: no question. Every entry must also be in ``FileReader.SUPPORTED`` -- the
#: allowlist NARROWS what a scan takes, it can never widen it to an extension
#: no reader handles.
DOC_EXTENSIONS = frozenset({".md", ".pdf", ".docx", ".org"})

#: Below this, a file is a stub, a redirect, or a badge header -- not a
#: document worth an extraction call.
MIN_DOC_BYTES = 2048

#: Directory names pruned on top of ``HARD_SKIP_DIRS``. Dot-directories
#: (``.github``, ``.cache``) are already pruned by the ``startswith(".")`` rule
#: in ``FolderWatcher._walk``, so they are not repeated here.
SKIP_DIRS = frozenset({
    # Scratch space: real prose, but not the project's documentation.
    "tmp", "temp", "scratch",
    # Third-party trees -- somebody else's docs.
    "_vendor", "vendor", "site-packages",
    # Test data and recorded output: machine-readable by definition.
    "fixtures", "testdata", "test-data", "snapshots", "__snapshots__",
    "coverage", "htmlcov",
    # Translation catalogues and schema migrations: generated, not authored.
    "locales", "i18n", "migrations",
    # Sample code, and agent instruction trees (already in agent context).
    "examples", "skills", "builtin_skills",
})

#: Patterns matched against the path RELATIVE TO THE PROJECT ROOT, which is
#: what makes root-anchoring expressible: a pattern containing no separator can
#: only match a top-level path.
IGNORE_PATTERNS: tuple[str, ...] = (
    # Agent instructions at any depth. The agent already has these in context,
    # so ingesting them teaches the Library nothing and pollutes retrieval.
    "*SKILL.md",
    # Packaging metadata. The dot is mid-name, so the dot-prefix directory
    # pruning in _walk never sees these.
    "*.egg-info/*",
    # Repository boilerplate, ROOT-ANCHORED. Anchoring is the single most
    # important property of this list: as bare basenames these names also match
    # real documents deeper in the tree -- measured against this repository,
    # `security.md` alone matches two nested documents several directories down.
    # Because these patterns carry no
    # "/", fnmatch can only match a top-level relative path, so the repository's
    # own SECURITY.md is dropped while nested security docs survive.
    "AGENTS.md",
    "CLAUDE.md",
    "KIRO.md",
    "GEMINI.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "AUTHORS.md",
    "NOTICE.md",
    "CODEOWNERS",
    "LICENSE*",
)


def project_doc_properties() -> dict:
    """Source ``properties`` that restrict a folder scan to documents.

    Merged into an auto-registered source's properties, so the ordinary
    ``FolderWatcher`` scan path applies the rule. Lists (not sets) because the
    properties blob is JSON.
    """
    return {
        "include_extensions": sorted(DOC_EXTENSIONS),
        "ignore_patterns": list(IGNORE_PATTERNS),
        "extra_skip_dirs": sorted(SKIP_DIRS),
        "min_file_bytes": MIN_DOC_BYTES,
    }


def should_ingest_doc(rel_path: str, size: int) -> bool:
    """True when *rel_path* is an auto-addable document.

    *rel_path* is relative to the project root -- absolute paths and paths
    containing ``..`` cannot be judged, because root-anchoring is meaningless
    without a known root, so they are refused.

    This is the same rule :func:`project_doc_properties` hands to a scan, kept
    callable so it can be unit-tested per path and run over a tree to measure
    what a scan would take.
    """
    norm = rel_path.replace(os.sep, "/")
    # Strip a single leading "./" -- NOT with lstrip("./"), which strips a
    # character class and would turn ".github/x.md" into "github/x.md" and
    # "/abs/x.md" into "abs/x.md", silently defeating both the dot-directory
    # rule and the absolute-path refusal below.
    if norm.startswith("./"):
        norm = norm[2:]
    if not norm or norm.startswith("/") or ".." in norm.split("/"):
        return False
    parts = norm.split("/")
    dirs, name = parts[:-1], parts[-1]
    if any(d in SKIP_DIRS or d in HARD_SKIP_DIRS or d.startswith(".") for d in dirs):
        return False
    if any(fnmatch(name.lower(), pat) for pat in DEFAULT_IGNORE_GLOBS):
        return False
    if Path(name).suffix.lower() not in DOC_EXTENSIONS:
        return False
    if any(fnmatch(norm, pat) for pat in IGNORE_PATTERNS):
        return False
    return size >= MIN_DOC_BYTES
