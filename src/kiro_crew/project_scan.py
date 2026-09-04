"""Project scanner — detect the packages in a directory tree, and nothing else.

A chat folder already carries a ``project_dir`` and nests via ``parent_id``, so a
chat opened inside one is steering- and scope-correct for its package. What is
missing for a monorepo, or a directory of sibling repositories, is *population*:
assembling N sub-folders by hand is work nobody does, so per-package steering
never loads. This module supplies the detection half of that — the folder tree is
then built by composing the existing folder API, which stays the only writer.

Two properties define the module and are worth stating up front:

* **Read-only and pure.** Detection depends on the filesystem and the passed
  configuration, and on nothing else: no writes, no state, no editor workspace
  artifacts. That is what makes a scan safe to point at an unfamiliar tree, and
  what makes :class:`CandidateTree` reproducible across runs. The only files
  *opened* are workspace declarations (see :data:`WORKSPACE_DECLARATIONS`) and
  ``.gitignore`` files, and only when found as regular files while walking —
  never through a link. A ``.gitignore`` is filesystem content like everything
  else here, so honouring it keeps the scan a pure function of the tree: two
  scans of an unchanged tree still compare equal.
* **Prune beats every signal.** A name in :data:`PRUNE_DIRS` is rejected before
  it is ever classified, so a vendored ``node_modules/left-pad/package.json``
  cannot become a candidate no matter how well it matches. The precedence is
  one-directional on purpose — the alternative (classify, then filter) leaks a
  candidate the moment a new signal is added without a matching filter.
* **Gitignored is pruned.** The project's own ``.gitignore`` already says which
  trees are not its source, so the scan believes it instead of enumerating
  every build tool's directory name: an ignored directory is never entered and
  never classified — a ``.git`` inside it cannot rescue it, which is exactly
  what defeats the SwiftPM/Xcode shape (``tmp/derived_data/SourcePackages/
  checkouts/`` holds every dependency as a full clone) — and an ignored file
  contributes nothing, neither a manifest signal nor a workspace declaration.
  Matching follows git semantics (nested files stack, deeper files win,
  ``!negation`` re-includes) via ``pathspec``. Only ``.gitignore`` files at or
  below the scan root are read: a parent directory's file is outside the tree
  the user pointed at, and the scanner never reads outside that tree.

Classification is two-tiered rather than boolean because the confidence differs:
a repository or manifest at the top of the scan root is what the user pointed at,
while a directory *inside* a package may be a real sub-package or may be an
implementation detail. So the tree offers both and lets the preview decide what
is ticked by default — see :class:`Tier`.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import json
import os
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence

import pathspec
import yaml  # type: ignore[import-untyped]

from kiro_crew.hooks import safe_read_file_bytes_nolink
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls

# `tomllib` is stdlib only from 3.11, and this project supports 3.10. The
# `tomli` backport is not a declared dependency, so neither may be importable —
# see `_cargo_members` for what happens then.
try:
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]

# Directory names that mark a package boundary by themselves.
GIT_DIR = ".git"
KIRO_DIR = ".kiro"

# The one file honoured as an exclusion source. Read only when found as a
# regular file at or below the scan root — see the module docstring.
GITIGNORE_FILE = ".gitignore"

# Filenames recognized as a package manifest. Widening this tuple is a code
# change on purpose: a name that marks a package for everyone belongs here,
# where it arrives with a test and a reviewer.
MANIFESTS: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "build.sbt",
    "pubspec.yaml",
    "composer.json",
    "Gemfile",
    "mix.exs",
    "Package.swift",
    "deno.json",
    "deno.jsonc",
    # Deploy-root markers. Not build manifests, but they mark the same thing
    # one does — "this directory is a deployable unit" — and such an app
    # commonly has no manifest of its own at that level (a Firebase app's
    # package.json files live in its functions/ and web/ children). Generic
    # names that would flag non-apps (template.yaml, app.yaml, Dockerfile)
    # are deliberately absent.
    "firebase.json",
    "vercel.json",
    "netlify.toml",
    "amplify.yml",
    "serverless.yml",
    "serverless.yaml",
    "cdk.json",
    "wrangler.toml",
    "wrangler.jsonc",
    "fly.toml",
    "render.yaml",
    "Procfile",
)

# The deploy-root subset of :data:`MANIFESTS`. Split out because the tier rule
# treats them differently: a nested build manifest may be a build fixture, but
# a deploy config never is — someone deploys that directory — so these stay
# unambiguous at any depth, like a repository.
DEPLOY_ROOTS: frozenset[str] = frozenset(
    (
        "firebase.json",
        "vercel.json",
        "netlify.toml",
        "amplify.yml",
        "serverless.yml",
        "serverless.yaml",
        "cdk.json",
        "wrangler.toml",
        "wrangler.jsonc",
        "fly.toml",
        "render.yaml",
        "Procfile",
    )
)

# Files that can carry a workspace's member list. ``package.json`` and
# ``Cargo.toml`` are manifests too; the other two mean nothing on their own —
# they are read for their member lists but never make their directory a
# candidate, because a member list is a statement about *other* directories.
DECL_NPM = "package.json"
DECL_PNPM = "pnpm-workspace.yaml"
DECL_CARGO = "Cargo.toml"
DECL_GO_WORK = "go.work"
WORKSPACE_DECLARATIONS: tuple[str, ...] = (DECL_NPM, DECL_PNPM, DECL_CARGO, DECL_GO_WORK)

# A member list is hand-written and names directories; a file this large is
# something else that happens to share the name. Capping the read keeps a scan
# bounded in bytes as well as in depth, and refusing the outsized file is more
# honest than parsing a truncated prefix of it.
MAX_DECLARATION_BYTES = 512 * 1024

# Reasons quoted in a warning are trimmed to this: a parser's message can embed
# the offending source line, and a warning is rendered in a preview UI.
_MAX_WARNING_REASON = 200

# Dependency, build-output, and virtual-environment directories: never traversed
# and never classified. Dot-directories are pruned by the rule in
# :func:`is_pruned` instead of being enumerated here, because the interesting set
# is open-ended (every tool adds one) while the exception is a single name.
#
# ``env``, ``venv`` and ``.venv`` are the three conventional names a Python
# virtual environment is created under, and they are the same class of directory
# as ``node_modules``: a vendored tree holding one third-party manifest per
# installed package. One environment therefore offers dozens of directories that
# look like packages and are not, which is why the names are pruned rather than
# left to the classifier. ``.venv`` is also covered by the dot-directory rule in
# :func:`is_pruned`; it is listed here as well so the three spellings of one
# concept read together.
PRUNE_DIRS: tuple[str, ...] = (
    "node_modules",
    "dist",
    "build",
    "target",
    "env",
    "venv",
    ".venv",
    "__pycache__",
    # Xcode's build directory is the same class again: SwiftPM vendors every
    # dependency as a full git clone under DerivedData/SourcePackages/checkouts,
    # so one iOS project offers dozens of repositories that are not the user's
    # packages. The name-prune is the belt for projects scanned without a
    # .gitignore; projects that have one are covered by the gitignore rule too.
    "DerivedData",
)

# Depth below the scan root that traversal will not pass. A cap is what keeps a
# scan bounded on a tree whose shape is unknown; 5 covers the layouts this
# feature exists for (``root/packages/<pkg>/...``) without inviting a walk of a
# whole home directory.
DEFAULT_DEPTH_CAP = 5

# Signal names recorded on a candidate. They are part of the preview payload —
# the UI explains *why* a directory is offered — so they are constants rather
# than literals sprinkled through the walker.
SIGNAL_GIT = "git"
SIGNAL_KIRO = KIRO_DIR
SIGNAL_MEMBER = "member"
_MANIFEST_SIGNAL_PREFIX = "manifest:"


def manifest_signal(filename: str) -> str:
    """Return the signal name recording that ``filename`` was found."""

    return f"{_MANIFEST_SIGNAL_PREFIX}{filename}"


def recognized_manifests() -> frozenset[str]:
    """Return the manifest filenames that mark a directory as a package."""

    return frozenset(MANIFESTS)


def is_pruned(name: str) -> bool:
    """Return whether a directory named ``name`` must be pruned.

    Covers :data:`PRUNE_DIRS` plus any dot-directory other than ``.kiro``, which
    is the one hidden directory carrying a detection signal. Callers must consult
    this *before* classifying, never after: prune precedence is only real if a
    pruned name never reaches the classifier.
    """

    if name in PRUNE_DIRS:
        return True
    return name.startswith(".") and name != KIRO_DIR


class DeclarationError(ValueError):
    """A workspace declaration was found but could not be understood.

    Always recoverable: the declaration is skipped and the scan continues with a
    warning. A project that cannot express its members is still a project whose
    other packages the user wants offered.
    """


def _string_list(value: object, *, where: str) -> list[str]:
    """Return the non-blank strings in ``value``, requiring ``value`` to be a list.

    A non-string entry is dropped rather than raising: one nonsense entry in an
    otherwise readable member list should cost that entry, not the whole list.
    The list type itself is load-bearing, though — a ``members = "crates/*"``
    string would otherwise be iterated character by character.
    """

    if not isinstance(value, list):
        raise DeclarationError(f"{where} is not a list")
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _npm_workspaces(text: str) -> list[str]:
    """Return the member patterns declared by a ``package.json``.

    Covers both shapes in the wild: npm's and pnpm's ``"workspaces": [...]`` and
    yarn's ``"workspaces": {"packages": [...]}``. A ``package.json`` without the
    key is the common case and is not an error — most of them are plain
    packages.
    """

    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        # RecursionError as well as a parse error, for the same reason the pnpm
        # parser guards it: a few KB of deeply nested arrays exhausts the parser
        # stack while sitting well under the size cap, and the file is tree
        # content the user merely pointed at — it must cost a warning, not the
        # scan.
        raise DeclarationError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DeclarationError("top level is not an object")
    declared = data.get("workspaces")
    if declared is None:
        return []
    if isinstance(declared, dict):
        packages = declared.get("packages")
        return [] if packages is None else _string_list(packages, where="workspaces.packages")
    return _string_list(declared, where="workspaces")


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML aliases.

    ``safe_load`` expands ``*alias`` references, so a small file can compose a
    graph orders of magnitude larger than itself. A scan reads whatever
    ``pnpm-workspace.yaml`` it finds in a tree the user merely pointed at, and no
    real member list needs an alias, so the amplification vector is closed
    outright instead of bounded. A lone anchor with nothing referencing it is
    harmless and stays allowed.
    """

    def compose_node(self, parent: object, index: object) -> object:
        if self.check_event(yaml.events.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None, None, "found alias, which is not allowed", event.start_mark
            )
        return super().compose_node(parent, index)


def _load_no_alias_yaml(text: str) -> object:
    """Parse ONE YAML document with :class:`_NoAliasLoader`.

    Drives the loader instance directly rather than handing this subclass to the
    module-level ``Loader=`` helper: the parse is identical, but the SafeLoader
    subclass becomes the only construction path, so neither a reader nor a
    scanner keyed on the call name has to infer the safety of the parse from an
    argument.
    """

    loader = _NoAliasLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _pnpm_packages(text: str) -> list[str]:
    """Return the member patterns declared by a ``pnpm-workspace.yaml``."""

    try:
        data = _load_no_alias_yaml(text)
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        # RecursionError as well as a parse error: nesting deep enough to exhaust
        # the stack is a malformed member list by any useful definition, and it
        # must cost this one declaration rather than the scan.
        raise DeclarationError(f"invalid YAML: {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise DeclarationError("top level is not a mapping")
    packages = data.get("packages")
    return [] if packages is None else _string_list(packages, where="packages")


def _cargo_members(text: str) -> list[str]:
    """Return the member patterns declared by a ``Cargo.toml``.

    Uses a real TOML parser when the interpreter has one. Where it does not
    (3.10 without the ``tomli`` backport) it falls back to
    :func:`_scan_cargo_members` rather than skipping Cargo workspaces, because a
    whole ecosystem going undetected on one supported interpreter is a worse
    outcome than a narrower parse.
    """

    if _toml is None:  # pragma: no cover - only on 3.10 without tomli
        return _scan_cargo_members(text)
    try:
        data = _toml.loads(text)
    except (ValueError, RecursionError) as exc:  # TOMLDecodeError is a ValueError
        # RecursionError for the same reason as the npm and pnpm parsers: deep
        # nesting exhausts the parser stack under the size cap, and a malformed
        # file costs a warning, never the scan.
        raise DeclarationError(f"invalid TOML: {exc}") from exc
    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        return []
    members = workspace.get("members")
    return [] if members is None else _string_list(members, where="workspace.members")


_TOML_TABLE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]")
_TOML_STRING = re.compile(r"\"([^\"]*)\"|'([^']*)'")


def _scan_cargo_members(text: str) -> list[str]:
    """Extract ``[workspace] members`` from Cargo manifest text without a parser.

    Deliberately narrow: it recognizes the one shape Cargo itself writes — an
    array of quoted strings assigned to ``members`` inside the ``[workspace]``
    table, optionally spread across lines — and ignores everything else,
    including a ``#`` inside a string. It also cannot detect a malformed file,
    which is why a real parser is preferred wherever one is importable.
    """

    members: list[str] = []
    in_workspace = False
    collecting = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        table = _TOML_TABLE.match(line)
        if table:
            # Any other table ends the workspace table, including a sub-table
            # such as [workspace.dependencies], whose keys are not members.
            in_workspace = table.group(1) == "workspace"
            collecting = False
            continue
        if not in_workspace:
            continue
        if not collecting:
            key, separator, remainder = line.partition("=")
            if not separator or key.strip() != "members":
                continue
            collecting = True
            line = remainder
        members.extend(
            double or single for double, single in _TOML_STRING.findall(line) if double or single
        )
        if "]" in line:
            collecting = False
    return [member.strip() for member in members if member.strip()]


_GO_USE = re.compile(r"^use\b\s*(.*)$")


def _go_work_uses(text: str) -> list[str]:
    """Return the module directories a ``go.work`` file uses.

    ``go.work`` is a line-oriented format with two spellings of the same thing —
    ``use ./api`` and a parenthesized block of directories — so a line reader is
    the whole parser. ``go``, ``require``, and ``replace`` directives name
    versions rather than directories and are ignored.
    """

    members: list[str] = []
    in_block = False
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line.startswith(")"):
                in_block = False
                continue
            members.append(_unquote(line))
            continue
        match = _GO_USE.match(line)
        if not match:
            continue
        target = match.group(1).strip()
        if target.startswith("("):
            in_block = True
            continue
        if target:
            members.append(_unquote(target))
    return [member for member in members if member]


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes from a declaration entry."""

    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    return value


DECLARATION_PARSERS: Mapping[str, Callable[[str], list[str]]] = {
    DECL_NPM: _npm_workspaces,
    DECL_PNPM: _pnpm_packages,
    DECL_CARGO: _cargo_members,
    DECL_GO_WORK: _go_work_uses,
}


def declared_patterns(path: str, root: str) -> list[str]:
    """Return the member patterns a declaration file at ``path`` names.

    An empty list means "declares no members" — the overwhelmingly common case
    for a ``package.json`` or ``Cargo.toml``, which are manifests first.

    Args:
        path: the declaration file to read.
        root: the approved scan root. The read is refused unless the OPENED
            descriptor resolves inside it, which is what makes the refusal hold
            against a path swapped after the walk saw it.

    Raises:
        DeclarationError: if the file cannot be understood, sits inside a
            protected location, or cannot be read safely.
    """

    if is_sensitive_path(path):
        # Refused by name so the caller gets a precise reason. This check is not
        # sufficient on its own — a name says nothing about the inode it opens,
        # which is why the read below is descriptor-checked.
        raise DeclarationError("sits inside a protected location")

    raw = safe_read_file_bytes_nolink(
        path,
        within_root=root,
        # One byte past the cap distinguishes "at the limit" from "truncated";
        # allow_truncate keeps an oversized file readable so the size refusal
        # below owns that error instead of the reader raising its own.
        max_bytes=MAX_DECLARATION_BYTES + 1,
        allow_truncate=True,
    )
    if raw is None:
        # The reader refuses a non-regular file, a hardlinked one, a descriptor
        # escaping ``root``, and an unreadable one alike. A hardlink planted
        # inside the tree is the case the name check above cannot see: its path
        # is unremarkable while its inode is someone else's secret.
        raise DeclarationError("could not be read safely")
    if len(raw) > MAX_DECLARATION_BYTES:
        raise DeclarationError(f"larger than {MAX_DECLARATION_BYTES} bytes")
    # A member list is ASCII in practice; replacing undecodable bytes keeps a
    # stray one from costing the whole declaration.
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        # An empty file declares no members, which is an answer and not a parse
        # failure. Worth special-casing because a placeholder manifest is common
        # and every parser would otherwise call it malformed, filling the preview
        # with warnings about files that were never member lists.
        return []
    return DECLARATION_PARSERS[os.path.basename(path)](text)


@dataclass(frozen=True)
class _IgnoreLayer:
    """One ``.gitignore``'s patterns, tied to the directory that holds them.

    Git scopes a file's patterns to its own subtree, so a layer carries its
    base directory and every match is made against a path *relative to* that
    base. Layers are ordered shallow-to-deep and the deepest layer with an
    opinion wins, which is git's own precedence for nested files.
    """

    base: str
    spec: pathspec.GitIgnoreSpec


def _gitignore_spec(path: str, root: str) -> pathspec.GitIgnoreSpec:
    """Parse the ``.gitignore`` at ``path`` into a matcher.

    The same reading rules as :func:`declared_patterns`, because the risk is
    the same: the file is tree content the user merely pointed at, so it is
    size-capped rather than trusted, and undecodable bytes cost a line rather
    than the file.

    Raises:
        DeclarationError: if the file is larger than the declaration cap, holds
            a pattern the gitignore grammar refuses, sits inside a protected
            location, or cannot be read safely.
    """

    if is_sensitive_path(path):
        # Same two-part rule as declared_patterns: the name check gives a precise
        # refusal, and the descriptor check below is what actually holds.
        raise DeclarationError("sits inside a protected location")
    raw = safe_read_file_bytes_nolink(
        path,
        within_root=root,
        max_bytes=MAX_DECLARATION_BYTES + 1,
        allow_truncate=True,
    )
    if raw is None:
        raise DeclarationError("could not be read safely")
    if len(raw) > MAX_DECLARATION_BYTES:
        raise DeclarationError(f"larger than {MAX_DECLARATION_BYTES} bytes")
    text = raw.decode("utf-8", errors="replace")
    try:
        return pathspec.GitIgnoreSpec.from_lines(text.splitlines())
    except ValueError as exc:
        # pathspec refuses some patterns (a lone ``!``) with a ValueError
        # subclass. That is a property of the FILE, not of this process, so it
        # is converted to the same refusal an oversized file gets — the caller
        # already turns DeclarationError into a warning that costs the layer,
        # never the scan. Left uncaught it would surface as an HTTP 500 from a
        # tree the user merely pointed at.
        raise DeclarationError(f"invalid pattern: {exc}") from exc


def _ignored(path: str, *, is_dir: bool, layers: Sequence[_IgnoreLayer]) -> bool:
    """Return whether the applicable ``.gitignore`` layers ignore ``path``.

    ``layers`` arrive shallow-to-deep; each is consulted against the path
    relative to its own base (git scoping), directories match with the
    trailing-slash form so dir-only patterns (``tmp/``) behave, and the
    deepest layer that expresses an opinion — ignore or ``!``-re-include —
    decides. Git's "cannot re-include inside an excluded directory" rule is
    not re-implemented here because the walk enforces it structurally: an
    ignored directory is never entered, so nothing beneath it is ever asked
    about.
    """

    verdict = False
    for layer in layers:
        rel = os.path.relpath(path, layer.base)
        if rel.startswith(".."):
            continue
        rel_posix = rel.replace(os.sep, "/") + ("/" if is_dir else "")
        opinion = layer.spec.check_file(rel_posix).include
        if opinion is not None:
            verdict = opinion
    return verdict


def _ignored_by_tree(path: str, root: str, specs: Mapping[str, pathspec.GitIgnoreSpec]) -> bool:
    """Return whether any ancestor level's ``.gitignore`` prunes ``path``.

    The walk answers this incrementally for the directories it enters; this is
    the same judgement for a path that arrived by *name* instead — a workspace
    declaration's member — where each ancestor must be checked in turn, because
    a member inside an ignored directory is inside a tree the project already
    disowned. ``specs`` maps a directory to the parsed ``.gitignore`` it holds.
    """

    rel = os.path.relpath(path, root)
    if rel == "." or rel.startswith(".."):
        return False
    layers: list[_IgnoreLayer] = []
    current = root
    for part in rel.split(os.sep):
        spec = specs.get(current)
        if spec is not None:
            layers.append(_IgnoreLayer(base=current, spec=spec))
        current = os.path.join(current, part)
        if layers and _ignored(current, is_dir=True, layers=layers):
            return True
    return False


class Tier(Enum):
    """How confident detection is that a directory should become a folder.

    ``AUTO`` is ticked by default in the preview, ``OFFERED`` is shown unticked.
    A directory with no signal at all is absent from the tree entirely — that is
    the third outcome, and it is represented by omission rather than by a member
    here, so an ignored directory cannot be rendered by a surface that forgot to
    filter it out.
    """

    AUTO = "auto"
    OFFERED = "offered"


@dataclass(frozen=True)
class Candidate:
    """One detected package.

    Frozen because the tree is handed to preview, reconcile, and scaffold in
    turn: a later stage overlays its own state (``existing``, ``selected``) in
    its own payload instead of mutating detection output, which keeps the scan
    result comparable across runs.
    """

    # Absolute path, inside the scan root. Absoluteness is enforced below; being
    # inside the root is the walker's invariant, since only it knows the root.
    path: str
    # Display name — the directory's basename.
    name: str
    # Nearest ancestor that is itself a candidate; ``None`` means the candidate
    # hangs directly off the scan root.
    parent_path: str | None
    tier: Tier
    # Why this directory was detected, e.g. ``("git", "manifest:package.json")``.
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A relative path here would reach the folder API as a ``project_dir``
        # that resolves against whatever the process CWD happens to be, so it is
        # rejected at construction rather than several layers downstream.
        if not os.path.isabs(self.path):
            raise ValueError(f"candidate path must be absolute: {self.path!r}")
        if self.parent_path is not None and not os.path.isabs(self.parent_path):
            raise ValueError(f"candidate parent_path must be absolute: {self.parent_path!r}")


@dataclass(frozen=True)
class CandidateTree:
    """Everything one scan found, in an order that does not depend on the walk.

    Build via :meth:`build` rather than the constructor: ``candidates`` is
    documented as path-sorted, and directory iteration order is not guaranteed by
    the OS, so sorting is what makes two scans of an unchanged tree compare
    equal.
    """

    root: str
    candidates: tuple[Candidate, ...] = ()
    # Declarations skipped and subtrees that could not be read. A warning is how
    # this module reports a partial result: neither case aborts a scan, because
    # one unreadable directory should not cost the user the other twenty
    # packages.
    warnings: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        root: str,
        candidates: Iterable[Candidate],
        warnings: Iterable[str] = (),
    ) -> CandidateTree:
        """Return a tree with ``candidates`` normalized into path-sorted order.

        Warnings keep the order they were produced in: they are emitted by the
        walk itself, which is already deterministic, and their sequence carries
        the reading order a user follows.

        Raises:
            ValueError: if a candidate path is not inside ``root``.
        """

        ordered = tuple(sorted(candidates, key=lambda candidate: candidate.path))
        for candidate in ordered:
            # Containment is the invariant every later stage relies on: a
            # candidate path becomes a folder's ``project_dir``, so one that
            # escaped the root would scope a chat outside what the user pointed
            # at. The walker cannot produce one (it only joins child names onto
            # the root), but declaration parsing resolves paths a file supplied,
            # so the guard sits at the single point every tree is built through.
            if not _is_within(candidate.path, root):
                raise ValueError(f"candidate path {candidate.path!r} is outside scan root {root!r}")
        return cls(root=root, candidates=ordered, warnings=tuple(warnings))


def _is_within(path: str, root: str) -> bool:
    """Return whether ``path`` names a strict descendant of ``root``.

    A prefix comparison, not a ``commonpath`` call: both arguments are already
    normalized absolute paths, and ``commonpath`` raises on paths from different
    Windows drives — which is a legitimate "outside the root" answer, not an
    error. The separator is re-appended so ``/srv/app2`` does not read as being
    inside ``/srv/app``.
    """

    prefix = root.rstrip(os.sep) + os.sep
    return path.startswith(prefix) and path != prefix


# A directory's filesystem identity: ``(st_dev, st_ino)``. Recorded when a parent
# lists a child and re-checked when that child is opened, so the walk can tell
# "the directory I listed" from "whatever now answers to that name".
_DirId = tuple[int, int]


class DirectoryChangedError(OSError):
    """A directory was replaced between being listed and being read.

    An :class:`OSError` subclass on purpose: the walk already turns a directory
    it cannot read into a warning and keeps scanning, and a directory that was
    swapped underneath the scan is exactly that case — a subtree this scan can no
    longer vouch for, not a reason to fail the other twenty packages.
    """

    def __init__(self, directory: str) -> None:
        super().__init__(errno.ENOTDIR, "directory was replaced during the scan")
        self.directory = directory


# ``O_NOFOLLOW`` makes the open itself refuse a final component that has become a
# symlink, and ``O_DIRECTORY`` refuses one that is no longer a directory; neither
# exists on Windows, and ``os.scandir`` only accepts a descriptor on POSIX.
_NOFOLLOW_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
_PINNED_SCANDIR = hasattr(os, "O_NOFOLLOW") and os.scandir in os.supports_fd


def _verify_identity(directory: str, stat_result: os.stat_result, expect: _DirId | None) -> None:
    """Raise if ``directory`` is not the inode its parent listed.

    Args:
        directory: Path being read, for the error's message.
        stat_result: Stat of what was actually opened.
        expect: ``(st_dev, st_ino)`` recorded when the parent listed this
            directory (or, for the scan root, captured at scan entry), or
            ``None`` when no identity could be read — a failed or zero-inode
            stat — in which case there is nothing to compare against.

    Raises:
        DirectoryChangedError: if the identity does not match.
    """

    if expect is None:
        return
    # A zero inode is the platform saying it cannot identify the file, so there is
    # nothing to compare and a mismatch here would be an artifact rather than a
    # swap. Checked at the comparison too, not only where identities are captured,
    # so the invariant holds however the identity reached this call.
    if not expect[1]:
        return
    if (stat_result.st_dev, stat_result.st_ino) != expect:
        raise DirectoryChangedError(directory)


@contextlib.contextmanager
def _scandir_pinned(directory: str, expect: _DirId | None) -> Iterator[Iterable[os.DirEntry[str]]]:
    """Iterate ``directory``'s entries without ever reading through a swapped link.

    ``entry.is_dir(follow_symlinks=False)`` proves a child was a real directory
    when its parent was listed, but descending re-resolves the child by NAME, and
    a name is not an inode: a directory replaced by a symlink in between is read
    through that link, yielding entries from outside the scan root under a path
    string that still looks inside it — so containment, which compares path
    strings, cannot see the escape. Two guards close that window, and the second
    is what makes it airtight rather than merely narrower:

    * the open is ``O_NOFOLLOW``/``O_DIRECTORY``, so a *final* component that
      became a link fails the open instead of being followed, and the check and
      the use are then the same operation rather than two racing ones;
    * the opened descriptor's identity is compared against the one the parent
      recorded, which also catches the swap of an *ancestor* — that redirects the
      whole path, so what opens is a different inode than the one listed, even
      though the final component is still a real directory.

    Raises:
        OSError: if the directory cannot be opened or listed, including
            :class:`DirectoryChangedError` when it was replaced mid-scan.
    """

    if _PINNED_SCANDIR:
        fd = os.open(directory, _NOFOLLOW_DIR_FLAGS)
        try:
            _verify_identity(directory, os.fstat(fd), expect)
            with os.scandir(fd) as entries:
                yield entries
        finally:
            os.close(fd)
        return
    # Windows has neither flag and cannot scandir a descriptor. Two checks stand
    # in for the pinned open, and neither enumerates link kinds:
    #
    # * the identity re-check against what the parent recorded — usually inert
    #   here, because ``DirEntry.stat()`` reports a zero inode on this platform,
    #   which is recorded as "no identity";
    # * a resolution check on the LEAF: the directory's real path must be its
    #   parent's real path plus its own name, i.e. this final component does not
    #   redirect. Anchoring to the parent rather than the scan root keeps the
    #   check local and makes it inductive — every ancestor inside the walk
    #   passed the same check when it was the leaf being descended, and the scan
    #   root itself is the caller's to validate. A pre-planted junction or
    #   symlink is therefore refused no matter when it was created.
    #
    # What remains open is a swap landing between these checks and the read
    # below — stated plainly rather than reasoned away: this branch has no way
    # to make the check and the use one operation.
    _verify_identity(directory, os.lstat(directory), expect)
    parent, leaf = os.path.split(directory)
    if leaf and os.path.realpath(directory) != os.path.join(os.path.realpath(parent), leaf):
        raise DirectoryChangedError(directory)
    with os.scandir(directory) as entries:
        yield entries


@dataclass(frozen=True)
class _SubDir:
    """A child directory worth descending into, pinned to the inode listed.

    The identity travels with the name because the two are captured together, in
    the parent's single listing pass; recovering it later would mean re-statting
    the name, which is the very thing that cannot be trusted.
    """

    name: str
    # ``None`` when the platform cannot identify the child — its stat failed, or it
    # reported a zero inode (Windows does this for every ``DirEntry.stat()``).
    # Descent then falls back to whatever the platform's own guard provides rather
    # than comparing against an identity that was never real.
    identity: _DirId | None = None


@dataclass(frozen=True)
class _DirContents:
    """One directory's listing, reduced to the two things the walk needs."""

    # Sub-directories worth descending into: real directories only (never a
    # symlink or a junction), never a pruned name, and never ``.kiro`` — see
    # :func:`_read_dir`.
    subdirs: tuple[_SubDir, ...]
    # Detection signals the directory carries itself, in a fixed order so two
    # scans of an unchanged directory produce an equal candidate.
    signals: tuple[str, ...]
    # Names of workspace declaration files present as regular files, sorted.
    declarations: tuple[str, ...] = ()
    # Whether a ``.gitignore`` is present as a regular file — read (or refused)
    # by the caller, the same split as declarations: listing never opens files.
    has_gitignore: bool = False


@dataclass(frozen=True)
class _Frame:
    """A directory queued for classification, with the context of its ancestors."""

    path: str
    # Distance below the scan root; the root itself is 0 and is never a candidate
    # (the scaffold step creates the root's folder from the scan root directly).
    depth: int
    # Nearest ancestor that is itself a candidate; ``None`` means the scan root.
    parent_path: str | None
    # Whether any ancestor was detected as a package. This is what splits the
    # boundary rule from the nested rule: the same manifest means "this is the
    # package the user pointed at" outside a package and "this may be an
    # implementation detail of the package above" inside one.
    inside_package: bool
    # Every ``.gitignore`` in force here, shallow-to-deep — the ancestors' plus
    # this directory's own, applied to children before they are ever pushed.
    ignores: tuple[_IgnoreLayer, ...] = ()
    # ``(st_dev, st_ino)`` recorded when the parent listed this directory, so the
    # read can refuse an inode that is not the one detection was based on. The
    # scan root has no parent; it is pinned at scan entry to the inode the
    # caller's validation named, so an ancestor swapped afterwards cannot
    # redirect the whole walk.
    identity: _DirId | None = None


# The name-surrogate bit of a Windows reparse tag (``IsReparseTagNameSurrogate``
# in the Windows SDK): set on every reparse kind whose NAME resolves somewhere
# else — symlinks and mount points (junctions) today, and whatever redirecting
# kind ships next — and clear on the kinds that decorate a real local directory
# in place (cloud placeholders, WCI, dedup), which must keep scanning.
_REPARSE_NAME_SURROGATE = 0x2000_0000
# ``stat.FILE_ATTRIBUTE_REPARSE_POINT`` exists at runtime on every platform but
# typeshed declares it Windows-only, so the value is pinned here — the same
# treatment ``platform_compat`` gives it.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _entry_redirects(entry: os.DirEntry[str]) -> bool:
    """True when the listed entry's name resolves somewhere other than itself.

    ``is_dir(follow_symlinks=False)`` keeps a POSIX symlink out of the walk, but
    a Windows directory junction lstats as a plain directory and passes it —
    and ``mklink /J`` needs no elevation, so "creating a link needs privilege"
    covers only the symlink kind. Rather than enumerate link kinds (which would
    break again on the next one), this tests the property itself: a reparse
    point whose tag carries the name-surrogate bit redirects the name, so
    descending it would walk a tree the user never pointed at.

    Read from the entry's own listing-time stat, never from ``entry.path`` —
    under a descriptor ``scandir`` that attribute is a bare name a path-based
    probe would re-resolve against the CWD, and the listing's cached data is
    the answer the walk already trusts. On POSIX the attribute does not exist,
    so this is uniformly False there at no extra syscall (the identity capture
    below shares the same cached stat). A stat that fails here settles nothing;
    the descent-time guards own that case, exactly as they do for a missing
    identity.
    """

    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if not getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return bool(getattr(info, "st_reparse_tag", 0) & _REPARSE_NAME_SURROGATE)


def _entry_identity(entry: os.DirEntry[str]) -> _DirId | None:
    """Return the listed entry's ``(st_dev, st_ino)``, or ``None`` if unavailable.

    ``follow_symlinks=False`` so the identity is the directory itself rather than
    anything it might later be made to point at. A stat that fails here is not
    worth losing the subtree over: descent still has the ``O_NOFOLLOW`` guard, so
    ``None`` degrades the check rather than skipping the directory.

    A ZERO inode is treated as "no identity", not as an identity of zero. Windows
    fills ``st_ino``/``st_dev`` with zeros on a ``DirEntry.stat()`` (the values
    require a full ``os.stat``), so comparing that against the real inode a later
    ``os.lstat`` reports would mismatch for EVERY child and skip the whole tree.
    """

    try:
        stat_result = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat_result.st_ino:
        return None
    return (stat_result.st_dev, stat_result.st_ino)


def _read_dir(
    directory: str, manifests: frozenset[str], identity: _DirId | None = None
) -> _DirContents:
    """Read ``directory`` once, returning what to descend into, signal, and parse.

    Args:
        directory: Absolute path to list.
        manifests: Recognized manifest filenames.
        identity: ``(st_dev, st_ino)`` recorded when the parent listed this
            directory. Passing it is what makes the read refuse a directory that
            was swapped after detection; ``None`` skips the check, for the scan
            root that no parent listed.

    Raises:
        OSError: if the directory cannot be listed, including
            :class:`DirectoryChangedError` when it was replaced mid-scan. The
            caller turns that into a warning and keeps scanning; one unreadable
            directory must not cost the user the packages found elsewhere.
    """

    subdirs: list[_SubDir] = []
    has_git = False
    has_kiro = False
    found_manifests: list[str] = []
    found_declarations: list[str] = []
    has_gitignore = False
    with _scandir_pinned(directory, identity) as entries:
        for entry in entries:
            name = entry.name
            # ``follow_symlinks=False`` keeps a symlinked directory out of
            # ``subdirs``; ``_entry_redirects`` keeps out the link kinds that
            # lstat as plain directories anyway (a Windows junction, or any
            # future name-surrogate reparse kind), so what is appended is a
            # directory whose name resolves to itself. A refused entry falls
            # through to the NAME checks below — the same treatment a POSIX
            # symlink gets — so a link named like a manifest still signals by
            # name and a link named ``.git`` manufactures nothing. Listing
            # settles what this child IS right now; the identity recorded
            # alongside is what keeps that answer true at the moment the child
            # is finally opened, since by then only the NAME would otherwise
            # be re-resolved.
            if entry.is_dir(follow_symlinks=False) and not _entry_redirects(entry):
                if name == GIT_DIR:
                    has_git = True
                    continue
                if name == KIRO_DIR:
                    # A signal, not a container: Kiro's own directory holds
                    # steering and specs, so descending into it could only
                    # manufacture candidates out of the tool's own files.
                    has_kiro = True
                    continue
                # Prune before classify: a pruned name never reaches the
                # classifier because it never reaches the stack, so a vendored
                # manifest under a dependency directory cannot become a
                # candidate however well it matches.
                if is_pruned(name):
                    continue
                subdirs.append(_SubDir(name=name, identity=_entry_identity(entry)))
                continue
            if name in manifests:
                # Anything that is not a real directory and carries a manifest
                # name counts, including a symlinked manifest: only the name is
                # read, never the target, so containment is unaffected.
                found_manifests.append(name)
            # A declaration is the one thing the scan opens and reads, so unlike
            # a manifest it must be a regular file reached by walking — never a
            # link. Following one would read a file outside the tree the user
            # pointed at, and a parse error would quote its content back.
            if name in WORKSPACE_DECLARATIONS and entry.is_file(follow_symlinks=False):
                found_declarations.append(name)
            # Same rule for .gitignore, the other file the scan opens.
            if name == GITIGNORE_FILE and entry.is_file(follow_symlinks=False):
                has_gitignore = True

    signals: list[str] = []
    if has_git:
        signals.append(SIGNAL_GIT)
    if has_kiro:
        signals.append(SIGNAL_KIRO)
    signals.extend(manifest_signal(name) for name in sorted(found_manifests))
    # Sorted rather than in ``scandir`` order: directory iteration order is not
    # guaranteed, and the traversal order fixes the order warnings are reported
    # in (candidate order comes from :meth:`CandidateTree.build`).
    return _DirContents(
        subdirs=tuple(sorted(subdirs, key=lambda subdir: subdir.name)),
        signals=tuple(signals),
        declarations=tuple(sorted(found_declarations)),
        has_gitignore=has_gitignore,
    )


def _tier_for(signals: Sequence[str], *, inside_package: bool) -> Tier:
    """Return the tier for a directory carrying ``signals``.

    ``.git`` and ``.kiro`` are unambiguous at any depth — a nested repository is
    its own package, and a directory the user has already used with Kiro is one
    they have already treated as a project root. A deploy-root marker is
    unambiguous at any depth too: a build manifest inside a package may name a
    build fixture, but a deploy config never does — someone deploys that
    directory — so a monorepo's apps stay ticked even though the workspace root
    wraps them in a package. The remaining manifests are the ambiguous case,
    and position decides it: outside any package they name the package itself,
    inside one they are offered unticked.
    """

    if SIGNAL_GIT in signals or SIGNAL_KIRO in signals:
        return Tier.AUTO
    if any(
        signal.startswith(_MANIFEST_SIGNAL_PREFIX)
        and signal[len(_MANIFEST_SIGNAL_PREFIX) :] in DEPLOY_ROOTS
        for signal in signals
    ):
        return Tier.AUTO
    return Tier.OFFERED if inside_package else Tier.AUTO


_GLOB_MAGIC = ("*", "?", "[")
# A member pattern segment meaning "any number of directory levels".
_ANY_DEPTH = "**"


def _depth_of(path: str, root: str) -> int:
    """Return how many levels below ``root`` the path ``path`` sits."""

    relative = os.path.relpath(path, root)
    return 0 if relative == os.curdir else len(relative.split(os.sep))


def _child_dirs(
    directory: str, root: str, depth_cap: int, expect: _DirId | None
) -> list[tuple[str, _DirId | None]]:
    """Return the sub-directories of ``directory`` a member may be found in.

    The same four exclusions the walk applies, because member expansion must not
    become a way around them: a pruned or hidden name is never entered, a link
    is never crossed (``follow_symlinks=False`` plus the same name-surrogate
    gate that keeps a Windows junction out of the walk), the descent is
    descriptor-pinned
    so a directory swapped for a link after listing is refused rather than read
    through, and nothing past the depth cap is listed. Names are sorted so
    expansion order does not depend on the filesystem.

    Each child is returned with the identity its listing recorded, so the
    caller's next open can refuse a swap — the same name-to-identity chain the
    walk threads through ``_SubDir``.
    """

    if _depth_of(directory, root) >= depth_cap:
        return []
    try:
        # ``expect`` arrives from whoever listed this directory — the walk frame
        # for a declaring package, the previous expansion level for anything
        # deeper — so a directory swapped between listing and this open is
        # refused rather than read through.
        with _scandir_pinned(directory, expect) as entries:
            children = sorted(
                (
                    (entry.name, _entry_identity(entry))
                    for entry in entries
                    # The same gate the walk applies: a link that lstats as a plain
                    # directory (a Windows junction) must not be offered as a
                    # member, or its path — which resolves outside the tree the
                    # declaration lives in — would be minted as a candidate.
                    if entry.is_dir(follow_symlinks=False)
                    and not _entry_redirects(entry)
                    and not is_pruned(entry.name)
                    # ``.kiro`` survives ``is_pruned`` as a signal-bearing directory,
                    # but it is still not a place a workspace member lives.
                    and not entry.name.startswith(".")
                ),
                key=lambda item: item[0],
            )
    except OSError:
        # The walk records the warning for a directory it cannot read; here an
        # invisible member is simply a member that is not offered.
        return []
    return [(os.path.join(directory, name), identity) for name, identity in children]


def _descendants(
    directory: str, root: str, depth_cap: int, expect: _DirId | None
) -> list[tuple[str, _DirId | None]]:
    """Return ``directory`` and every directory beneath it within the cap."""

    found: list[tuple[str, _DirId | None]] = [(directory, expect)]
    index = 0
    while index < len(found):
        current, current_identity = found[index]
        index += 1
        found.extend(_child_dirs(current, root, depth_cap, current_identity))
    return found


def _segment_matches(name: str, pattern: str) -> bool:
    """Return whether a directory named ``name`` matches one pattern segment."""

    if any(char in pattern for char in _GLOB_MAGIC):
        # Case-sensitive even on a case-insensitive filesystem: two runs must
        # agree, and ``fnmatch`` alone would fold case using the host's rules.
        return fnmatch.fnmatchcase(name, pattern)
    return name == pattern


def _expand_member_pattern(
    base_dir: str,
    pattern: str,
    root: str,
    depth_cap: int,
    base_identity: _DirId | None,
) -> list[str]:
    """Return existing directories under ``base_dir`` matching ``pattern``.

    Patterns are resolved relative to the package that declared them, which is
    what npm, pnpm, Cargo, and go.work all mean by a member path.

    Expansion walks the tree segment by segment rather than calling
    :func:`glob.glob`, because it has to obey the scan's own limits: a pruned
    name is never entered, a link is never crossed, and nothing past the depth
    cap is reached. The stdlib glob honours none of the three, so filtering its
    output afterwards would still have paid the cost of walking a vendored
    dependency tree — and prune precedence would then depend on a filter, which
    is the ordering this module exists to avoid.

    ``base_identity`` is the identity the walk recorded for the declaring
    package, and every deeper open compares against the identity its own
    listing produced — expansion re-opens by name, and a name is not an inode,
    so without the chain a directory swapped after the walk would be read
    through here despite every guard the walk itself carries.
    """

    cleaned = pattern.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("~") or cleaned.startswith("/") or os.path.isabs(cleaned):
        # An absolute or home-relative member is not a path inside the declaring
        # package, so it cannot be inside the scan root's layout either.
        return []
    parts = [part for part in cleaned.split("/") if part not in ("", os.curdir)]
    if not parts or ".." in parts:
        # Climbing out of the declaring package either leaves the scan root or
        # names something reachable directly; a bare "." names the declarer.
        return []

    frontier: list[tuple[str, _DirId | None]] = [(base_dir, base_identity)]
    for part in parts:
        matched: list[tuple[str, _DirId | None]] = []
        for directory, identity in frontier:
            if part == _ANY_DEPTH:
                # Zero or more levels, exactly as a shell glob reads ``**``: it is
                # what makes "packages/**/tests" match "packages/tests", and one
                # rule beats a second rule for a trailing ``**``. The cost is that
                # "packages/**" also offers "packages" itself — an extra unticked
                # candidate, which the preview exists to reject.
                matched.extend(_descendants(directory, root, depth_cap, identity))
            else:
                matched.extend(
                    child
                    for child in _child_dirs(directory, root, depth_cap, identity)
                    if _segment_matches(os.path.basename(child[0]), part)
                )
        # ``**`` can reach the same directory from two frontier entries, and a
        # member offered twice would be a duplicate candidate.
        frontier = list(dict.fromkeys(matched))

    # Containment is re-checked rather than assumed: it is the guarantee the rest
    # of the feature is built on, and it costs one string comparison. A pattern
    # that resolves back to the declaring package is dropped — a package is not
    # its own member.
    return [path for path, _ in frontier if path != base_dir and _is_within(path, root)]


def _warning_reason(exc: BaseException) -> str:
    """Return a short, single-line reason for a warning message.

    Redacted at construction, not at a boundary: a parse error quotes the
    offending source line back, and that line is tree content the user merely
    pointed at — a malformed YAML declaration holding a credential would
    otherwise ride the warning into the scan response. Sanitizing here means
    no unredacted copy of the reason ever exists for a later consumer (a log
    line, a persisted report) to leak.
    """

    reason = str(getattr(exc, "strerror", None) or exc) or exc.__class__.__name__
    reason, _ = redact_exfiltration_urls(reason)
    reason, _ = redact_credentials(reason)
    reason = " ".join(reason.split())
    if len(reason) > _MAX_WARNING_REASON:
        reason = reason[:_MAX_WARNING_REASON] + "…"
    return reason


def _declared_members(
    directory: str,
    declarations: Sequence[str],
    root: str,
    depth_cap: int,
    warnings: list[str],
    identity: _DirId | None,
) -> list[str]:
    """Return the member directories declared by files in ``directory``.

    A declaration that cannot be read or parsed adds a warning and is skipped:
    the packages found by walking are worth more to the user than an error page.

    ``identity`` is what the walk recorded for ``directory`` — expansion
    re-opens it by name, and the recorded inode is what refuses a swap landing
    between the walk's read and this one.
    """

    members: list[str] = []
    for filename in declarations:
        file_path = os.path.join(directory, filename)
        try:
            patterns = declared_patterns(file_path, root)
        except (OSError, DeclarationError) as exc:
            warnings.append(
                f"skipped workspace declaration {file_path}: {_warning_reason(exc)}",
            )
            continue

        # npm and pnpm both spell an exclusion "!pattern". Honouring it matters
        # because an excluded directory is one the project has already said is
        # not a member; offering it anyway would contradict the file we just
        # read. Exclusions are expanded the same way, then subtracted.
        included: list[str] = []
        excluded: set[str] = set()
        for pattern in patterns:
            negated = pattern.startswith("!")
            expanded = _expand_member_pattern(
                directory, pattern[1:] if negated else pattern, root, depth_cap, identity
            )
            if negated:
                excluded.update(expanded)
            else:
                included.extend(expanded)
        members.extend(path for path in included if path not in excluded)
    return members


def _nearest_ancestor(path: str, candidates: Mapping[str, Candidate], root: str) -> str | None:
    """Return the closest ancestor of ``path`` that is itself a candidate."""

    current = os.path.dirname(path)
    while _is_within(current, root):
        if current in candidates:
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _with_members(
    candidates: Sequence[Candidate], member_paths: Iterable[str], root: str
) -> list[Candidate]:
    """Fold declared members into the candidates the walk found.

    A member is offered unticked, never ticked: a member list says the directory
    belongs to a workspace, which is weaker evidence of "the user wants a folder
    for this" than the directory's own repository or ``.kiro``. A member the walk
    already found therefore keeps the tier it earned — being named in a list does
    not demote an auto-selected candidate — and only gains the member signal, so
    a preview can show both reasons.
    """

    by_path: dict[str, Candidate] = {candidate.path: candidate for candidate in candidates}
    for path in sorted(set(member_paths)):
        found = by_path.get(path)
        if found is not None:
            if SIGNAL_MEMBER not in found.signals:
                by_path[path] = replace(found, signals=found.signals + (SIGNAL_MEMBER,))
            continue
        by_path[path] = Candidate(
            path=path,
            name=os.path.basename(path),
            parent_path=None,
            tier=Tier.OFFERED,
            signals=(SIGNAL_MEMBER,),
        )

    # Parents are derived from the final path set instead of kept from the walk:
    # a member whose only signal is the declaration can sit *between* two walked
    # candidates, and the walk assigned their parents before it existed. With no
    # members this reproduces exactly what the walk recorded.
    return [
        replace(candidate, parent_path=_nearest_ancestor(path, by_path, root))
        for path, candidate in by_path.items()
    ]


def scan(
    root: Path,
    *,
    depth_cap: int = DEFAULT_DEPTH_CAP,
) -> CandidateTree:
    """Walk ``root`` read-only and return the packages found beneath it.

    Detection has two sources. Walking finds a directory's own signals — a
    repository, a ``.kiro`` directory, a manifest. Reading the workspace
    declarations found on the way (``workspaces``, ``pnpm-workspace.yaml``,
    Cargo ``[workspace] members``, ``go.work``) adds the members a package names
    for itself, which is how a member directory that carries no manifest of its
    own still gets offered.

    Args:
        root: Absolute directory to scan. Callers validate it with the folder
            API's own path validation first, so the scan refuses exactly what
            manual folder creation refuses.
        depth_cap: Maximum depth below ``root`` to descend.

    Returns:
        A :class:`CandidateTree`; empty candidates is a valid answer, not an
        error, and a declaration or subtree that could not be read is reported in
        its warnings rather than raised.
    """

    # Not ``resolve()``: the recorded paths stay the ones the caller named, so a
    # root reached through a symlinked parent yields folders whose ``project_dir``
    # matches what the user typed. Containment holds regardless, because every
    # deeper path is this string with child names joined onto it.
    root_path = os.path.abspath(os.fspath(root))
    # Pin the root to the inode it names right now. Every other frame gets its
    # identity from the parent's listing; the root has no parent, and leaving it
    # unpinned would let an ancestor swapped after the caller's validation
    # redirect the whole walk into a tree the user never pointed at. A failed
    # lstat records no identity — the first read then fails on the same
    # condition and reports the root unreadable.
    root_identity: _DirId | None = None
    try:
        root_stat = os.lstat(root_path)
    except OSError:
        pass
    else:
        if root_stat.st_ino:
            root_identity = (root_stat.st_dev, root_stat.st_ino)
    manifests = recognized_manifests()

    candidates: list[Candidate] = []
    members: list[str] = []
    warnings: list[str] = []
    # Every .gitignore parsed during the walk, keyed by the directory holding
    # it — the input _ignored_by_tree needs to give member paths (which arrive
    # by name, not by walking) the same pruning the walk applies structurally.
    gitignore_specs: dict[str, pathspec.GitIgnoreSpec] = {}
    stack = [
        _Frame(
            path=root_path,
            depth=0,
            parent_path=None,
            inside_package=False,
            identity=root_identity,
        )
    ]

    while stack:
        frame = stack.pop()
        try:
            contents = _read_dir(frame.path, manifests, frame.identity)
        except OSError as exc:
            # A subtree we cannot list is a partial result, not a failed scan.
            # That covers a directory swapped underneath us
            # (:class:`DirectoryChangedError`): detection was based on an inode
            # this path no longer names, so the honest outcome is to report the
            # subtree as unread rather than to classify whatever replaced it.
            warnings.append(f"skipped unreadable directory {frame.path}: {exc.strerror or exc}")
            continue

        # This directory's own .gitignore joins the layers in force before
        # anything here is judged: its patterns govern its own files (git
        # semantics), so a manifest it ignores must not count as a signal. An
        # unreadable or oversized file costs the layer, never the scan.
        layers = frame.ignores
        if contents.has_gitignore:
            gitignore_path = os.path.join(frame.path, GITIGNORE_FILE)
            try:
                spec = _gitignore_spec(gitignore_path, root_path)
            except (OSError, DeclarationError) as exc:
                warnings.append(f"skipped {gitignore_path}: {_warning_reason(exc)}")
            else:
                gitignore_specs[frame.path] = spec
                layers = layers + (_IgnoreLayer(base=frame.path, spec=spec),)

        # An ignored file contributes nothing: not a manifest signal, not a
        # workspace declaration. Repository/.kiro signals are directories and
        # stay — git itself never treats a repository's .git as ignorable.
        signals = contents.signals
        declarations = contents.declarations
        if layers:
            signals = tuple(
                signal
                for signal in signals
                if not signal.startswith(_MANIFEST_SIGNAL_PREFIX)
                or not _ignored(
                    os.path.join(frame.path, signal[len(_MANIFEST_SIGNAL_PREFIX) :]),
                    is_dir=False,
                    layers=layers,
                )
            )
            declarations = tuple(
                name
                for name in declarations
                if not _ignored(os.path.join(frame.path, name), is_dir=False, layers=layers)
            )

        # Declarations are read wherever they are found, including at a scan root
        # that is not itself a package: a `go.work` or `pnpm-workspace.yaml` in a
        # directory of repositories still names the members the user cares about.
        if declarations:
            members.extend(
                _declared_members(
                    frame.path, declarations, root_path, depth_cap, warnings, frame.identity
                )
            )

        parent_path = frame.parent_path
        # The root's own signals count even though the root is never a candidate:
        # pointing at a monorepo whose top level holds a manifest means every
        # package below it is nested inside that package, which is the case
        # Requirement 2's "shown unticked" tier exists for. Requirement 1's
        # boundary tier is for the other shape — a root that is just a directory
        # holding unrelated repositories.
        inside_package = frame.inside_package or bool(signals)
        if frame.depth > 0 and signals:
            candidates.append(
                Candidate(
                    path=frame.path,
                    name=os.path.basename(frame.path),
                    parent_path=frame.parent_path,
                    tier=_tier_for(signals, inside_package=frame.inside_package),
                    signals=signals,
                )
            )
            # Children hang off this candidate, not off whatever is above it.
            parent_path = frame.path

        # A directory at exactly the cap is still classified — it is within the
        # depth the caller allowed — but its children are one step too deep.
        if frame.depth >= depth_cap:
            continue

        # Reversed, because a stack pops last-in first: pushing in reverse walks
        # children alphabetically. Candidate order does not depend on this, but
        # warning order does, and a scan that reports its problems in a different
        # sequence each run is not reproducible.
        for subdir in reversed(contents.subdirs):
            child_path = os.path.join(frame.path, subdir.name)
            # Gitignored is pruned, with the same one-directional precedence as
            # PRUNE_DIRS: an ignored directory is never pushed, so it is never
            # entered and never classified — a .git inside cannot rescue it.
            if layers and _ignored(child_path, is_dir=True, layers=layers):
                continue
            stack.append(
                _Frame(
                    path=child_path,
                    depth=frame.depth + 1,
                    parent_path=parent_path,
                    inside_package=inside_package,
                    ignores=layers,
                    identity=subdir.identity,
                )
            )

    # Members arrive by name rather than by walking, so they get the pruning
    # judgement the walk applied structurally: a member inside a gitignored
    # directory is inside a tree the project already disowned.
    if gitignore_specs:
        members = [
            path for path in members if not _ignored_by_tree(path, root_path, gitignore_specs)
        ]

    return CandidateTree.build(root_path, _with_members(candidates, members, root_path), warnings)
