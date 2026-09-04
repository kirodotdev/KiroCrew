"""Property tests for the project scanner, over randomly generated trees.

The fixture suites pin the layouts a reader can picture. These pin the ones
nobody would think to write down: a manifest inside a pruned directory that is
itself a declared member, a workspace declaration naming ``..``, a symlink
looping back at the scan root, a signal-bearing directory sitting one level past
the depth cap. The scanner's answer decides which folders get created from a tree
the user merely pointed at, so the six statements below are asserted universally
rather than at sampled layouts:

1. **Determinism** — two scans of an unchanged tree return equal trees, warnings
   included. Directory iteration order is not guaranteed by the OS, so this is
   the property that would break first and most silently.
2. **Containment** — every candidate is a real directory strictly inside the
   scan root, and stays inside it after resolving links. A candidate path becomes
   a folder's ``project_dir``, so one that escaped would scope a chat outside
   what the user pointed at.
3. **Prune precedence** — no candidate path passes through a dependency,
   build-output, or hidden directory, however good the signal beneath it.
4. **Tier soundness** — each candidate's tier and signals are re-derived from
   disk independently of the walker and must agree with it.
5. **Depth bound** — nothing deeper than the configured cap is offered, by the
   walk or by member expansion.
6. **Read-only** — a content snapshot of the whole temporary area, the area
   *outside* the scan root included, is unchanged by a scan.

Trees are generated with a name alphabet that deliberately collides with the
scanner's own vocabulary (``build``, ``dist``, ``node_modules``, ``.git``,
``.kiro``, every manifest filename) and with member lists that name globs,
escaping paths, and pruned directories. A generator drawn only from inert names
would exercise the walk and none of the rules.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.project_scan import (
    DECL_CARGO,
    DECL_GO_WORK,
    DECL_NPM,
    DECL_PNPM,
    GIT_DIR,
    KIRO_DIR,
    MANIFESTS,
    PRUNE_DIRS,
    SIGNAL_GIT,
    SIGNAL_KIRO,
    SIGNAL_MEMBER,
    CandidateTree,
    Tier,
    manifest_signal,
    scan,
)

# Filesystem work per example is orders of magnitude past Hypothesis' default
# per-example deadline. The shared profile in ``conftest`` already lifts it and
# caps the example count; restating the deadline here keeps this file correct if
# that profile is ever narrowed, while leaving ``max_examples`` to the profile so
# ``HYPOTHESIS_PROFILE=thorough`` still deepens these properties. Coverage of the
# rule space at the default count comes from biasing the generators below, not
# from spending more examples.
_SETTINGS = settings(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# Name given to every generated symlink. Absent from both alphabets below so a
# link can never collide with a generated directory or file.
_LINK_NAME = "linked"

# Directory names: a few inert ones, every name the scanner prunes, and the two
# directories that are themselves detection signals. Drawing ``.git``/``.kiro``
# as ordinary children is what makes a boundary appear at a random depth. The
# signal-bearing and pruned names sit early on purpose — Hypothesis favours low
# indices while it is still exploring small inputs, and these are the names whose
# rules the properties are about.
_DIR_NAMES = st.sampled_from(
    (
        "app",
        GIT_DIR,
        "core",
        KIRO_DIR,
        "packages",
        "node_modules",
        "pkg",
        "build",
        "crates",
        "dist",
        "libs",
        "target",
        "env",
        "venv",
        ".venv",
        "__pycache__",
        ".cache",
    )
)

# File names: every recognized manifest, both declaration-only formats, a name
# that is a manifest ONLY when configured as an extra signal, and inert files.
_FILE_NAMES = st.sampled_from(
    (
        DECL_NPM,
        DECL_CARGO,
        DECL_PNPM,
        DECL_GO_WORK,
        "pyproject.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "BUILD.bazel",
        "notes.txt",
    )
)

# Member patterns. Roughly a third name a directory that plausibly exists, a
# third are globs, and a third are the cases a declaration must not be able to
# reach: out of the root, into a pruned directory, or back at the declarer.
_MEMBER_PATTERNS = st.sampled_from(
    (
        "app",
        "core",
        "pkg",
        "packages/*",
        "crates/*",
        "libs/**",
        "*",
        "**",
        "!core",
        "does-not-exist",
        "../outside",
        "../outside/*",
        "..",
        ".",
        "/elsewhere/pkg",
        "~/pkg",
        "node_modules/*",
        "build/*",
    )
)

_LINK_KINDS = st.sampled_from(("none", "outside", "loop"))

# Extra manifest signals, including a blank entry: configuration reaches the
# scanner unsanitized, and a blank name must not become a match-everything rule.


# Inert file bodies, keyed by filename. Content only has to be plausible: none of
# these files is parsed, they exist to carry (or not carry) a name.
_INERT_TEXT = {
    "pyproject.toml": '[project]\nname = "generated"\n',
    "go.mod": "module example/generated\n",
    "pom.xml": "<project></project>\n",
    "build.gradle": "// generated\n",
    "BUILD.bazel": "# generated\n",
    "notes.txt": "generated\n",
}


@dataclass(frozen=True)
class _Dir:
    """One generated directory: its name, contents, and the link it carries."""

    name: str
    files: tuple[str, ...]
    # Member patterns written into whichever declaration files ``files`` names.
    members: tuple[str, ...]
    # Write syntactically invalid declarations instead of valid ones. A scan must
    # absorb these as warnings, so every property has to hold with them present.
    broken_declarations: bool
    # Use yarn's ``{"packages": [...]}`` nesting rather than npm's flat list.
    yarn_shape: bool
    # ``"outside"`` links out of the scan root, ``"loop"`` links back at it.
    link: str
    children: tuple[_Dir, ...]


def _dir_specs(depth: int) -> st.SearchStrategy[_Dir]:
    """Return a strategy for a directory tree at most ``depth`` levels deep.

    Explicitly recursive rather than :func:`hypothesis.strategies.recursive`
    because the bound that matters here is depth, not leaf count: the depth cap
    is one of the properties under test, so trees have to reliably reach past it.

    Every generated directory holds at least one file and at least one child (up
    to the depth bound), which is a deliberate departure from letting the sizes
    shrink freely: Hypothesis minimizes what it is not told to keep, and a
    generator of mostly-empty directories produces mostly-empty candidate trees,
    which satisfy all six properties while testing none of the rules.
    """

    children = (
        st.just(())
        if depth <= 0
        else st.lists(_dir_specs(depth - 1), min_size=1, max_size=2).map(tuple)
    )
    return st.builds(
        _Dir,
        name=_DIR_NAMES,
        files=st.lists(_FILE_NAMES, min_size=1, max_size=3).map(tuple),
        members=st.lists(_MEMBER_PATTERNS, min_size=1, max_size=3).map(tuple),
        broken_declarations=st.booleans(),
        yarn_shape=st.booleans(),
        link=_LINK_KINDS,
        children=children,
    )


# Deeper than the largest generated depth cap, so overflowing the cap is a case
# the generator reaches rather than one it can only reach by luck.
_TREES = _dir_specs(4)
_DEPTH_CAPS = st.integers(min_value=1, max_value=4)


def _declaration_text(filename: str, spec: _Dir) -> str:
    """Return the body to write for a workspace declaration file."""

    if spec.broken_declarations:
        return {
            DECL_NPM: "{ not json",
            DECL_CARGO: "[workspace\nmembers = [",
            DECL_PNPM: "packages:\n  - [unclosed\n",
            DECL_GO_WORK: "use (\n\t./app\n",
        }[filename]

    members = spec.members
    if filename == DECL_NPM:
        if not members:
            return json.dumps({"name": "generated"})
        declared: object = {"packages": list(members)} if spec.yarn_shape else list(members)
        return json.dumps({"name": "generated", "workspaces": declared})
    if filename == DECL_CARGO:
        if not members:
            return '[package]\nname = "generated"\n'
        listed = ", ".join(f'"{member}"' for member in members)
        return f"[workspace]\nmembers = [{listed}]\n"
    if filename == DECL_PNPM:
        if not members:
            return "packages: []\n"
        return "packages:\n" + "".join(f'  - "{member}"\n' for member in members)
    lines = "".join(f"\t{member}\n" for member in members)
    return f"go 1.22\n\nuse (\n{lines})\n" if members else "go 1.22\n"


def _file_text(filename: str, spec: _Dir) -> str:
    """Return the body to write for one generated file."""

    if filename in (DECL_NPM, DECL_CARGO, DECL_PNPM, DECL_GO_WORK):
        return _declaration_text(filename, spec)
    return _INERT_TEXT[filename]


def _try_symlink(target: Path, link: Path) -> None:
    """Create a symlink if this process may, and shrug if it may not.

    Creating one needs a privilege an unelevated Windows account lacks. Skipping
    the link there costs coverage of the no-traversal rule on that platform,
    which is the right trade against skipping all six properties.
    """

    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pass


def _materialize(directory: Path, spec: _Dir, *, root: Path, outside: Path) -> None:
    """Write ``spec`` to disk under ``directory``.

    Duplicate sibling names are collapsed to the first occurrence: the generator
    draws names from a small alphabet, so collisions are frequent, and resolving
    them here keeps the tree on disk a faithful function of the drawn spec.
    """

    directory.mkdir(parents=True, exist_ok=True)
    for filename in dict.fromkeys(spec.files):
        (directory / filename).write_text(_file_text(filename, spec), encoding="utf-8")
    if spec.link == "outside":
        _try_symlink(outside, directory / _LINK_NAME)
    elif spec.link == "loop":
        _try_symlink(root, directory / _LINK_NAME)
    seen: set[str] = set()
    for child in spec.children:
        if child.name in seen:
            continue
        seen.add(child.name)
        _materialize(directory / child.name, child, root=root, outside=outside)


_EXAMPLES = itertools.count()


def _build_tree(area: Path, spec: _Dir) -> tuple[Path, Path]:
    """Materialize one example, returning its scan root and the area beside it.

    Every example gets its own numbered directory under a session-scoped
    temporary area: Hypothesis reuses the fixture across examples, so sharing one
    directory would leak the previous tree into the next draw.

    The sibling ``outside`` directory holds detection signals of its own. Nothing
    in it may ever surface as a candidate — it is the target of every escaping
    symlink and escaping member pattern the generator produces, so a candidate
    there is proof that containment failed rather than merely unproven.
    """

    base = area / f"ex{next(_EXAMPLES)}"
    outside = base / "outside"
    (outside / GIT_DIR).mkdir(parents=True)
    (outside / DECL_NPM).write_text(json.dumps({"name": "outside"}), encoding="utf-8")
    (outside / "pkg").mkdir()
    (outside / "pkg" / DECL_CARGO).write_text('[package]\nname = "o"\n', encoding="utf-8")

    root = base / "root"
    _materialize(root, spec, root=root, outside=outside)
    return root, outside


@pytest.fixture(scope="session")
def scan_area(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped temporary area for generated trees.

    Session scope on purpose: a function-scoped fixture is created once for a
    whole Hypothesis run, not once per example, which Hypothesis rightly flags.
    Per-example isolation comes from :func:`_build_tree` instead.
    """

    return tmp_path_factory.mktemp("scan-properties")


def _manifest_names() -> set[str]:
    """Return the manifest filenames a scan recognizes."""

    return set(MANIFESTS)


def _own_signals(directory: str, manifest_names: set[str]) -> set[str]:
    """Return the detection signals ``directory`` carries, read from disk.

    A second implementation of the signal rules, deliberately: it is the oracle
    the walker's own answer is compared against, so it reads the filesystem
    rather than reusing anything the walker used.
    """

    signals: set[str] = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name == GIT_DIR:
                    signals.add(SIGNAL_GIT)
                elif entry.name == KIRO_DIR:
                    signals.add(SIGNAL_KIRO)
            elif entry.name in manifest_names:
                signals.add(manifest_signal(entry.name))
    return signals


def _has_signalled_ancestor(path: str, root: Path, manifest_names: set[str]) -> bool:
    """Return whether any directory from ``path``'s parent up to ``root`` signals.

    This is the position half of the tier rule: the same manifest means "the
    package the user pointed at" outside a package and "possibly an
    implementation detail" inside one, and the scan root counts as an ancestor.
    """

    root_path = os.path.normpath(str(root))
    current = os.path.dirname(path)
    while True:
        if _own_signals(current, manifest_names):
            return True
        if os.path.normpath(current) == root_path:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _snapshot(area: Path) -> dict[str, tuple[object, ...]]:
    """Return a content snapshot of everything under ``area``.

    Covers names, kinds, link targets, sizes, modification times, and file
    bytes — enough that a created, deleted, touched, or rewritten entry all
    show up as an inequality. Access times are excluded: reading a declaration
    legitimately updates them.
    """

    snapshot: dict[str, tuple[object, ...]] = {}
    for current, dirnames, filenames in os.walk(area):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = os.path.join(current, name)
            info = os.stat(path, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                snapshot[path] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                snapshot[path] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                snapshot[path] = ("file", info.st_size, info.st_mtime_ns, digest)
    return snapshot


@_SETTINGS
@given(spec=_TREES, depth_cap=_DEPTH_CAPS)
def test_two_scans_of_an_unchanged_tree_agree(
    scan_area: Path,
    spec: _Dir,
    depth_cap: int,
) -> None:
    """Scanning twice returns equal trees, in path-sorted candidate order.

    Directory iteration order is not guaranteed, and the scan's output feeds a
    preview the user is asked to confirm: a tree that reshuffles between the
    preview and the confirmation is a tree the confirmation does not describe.
    """

    root, _ = _build_tree(scan_area, spec)

    first = scan(root, depth_cap=depth_cap)
    second = scan(root, depth_cap=depth_cap)

    assert first == second
    paths = [candidate.path for candidate in first.candidates]
    assert paths == sorted(paths)
    # Distinct candidates, so a directory reached twice (a declared member the
    # walk also found, or a ``**`` pattern matching by two routes) is folded into
    # one entry rather than offered twice.
    assert len(paths) == len(set(paths))


@_SETTINGS
@given(spec=_TREES, depth_cap=_DEPTH_CAPS, pick=st.integers(0, 7))
def test_gitignoring_a_child_prunes_exactly_that_subtree(
    scan_area: Path,
    spec: _Dir,
    depth_cap: int,
    pick: int,
) -> None:
    """A ``.gitignore`` naming a root child removes that subtree and nothing new.

    Whatever tree the generator built: after ignoring one child directory of
    the root, no candidate sits at or under it (a repository inside cannot
    rescue it), every surviving candidate existed before (an exclusion cannot
    manufacture packages), and the scan stays deterministic — the ``.gitignore``
    is filesystem input like everything else.
    """

    root, _ = _build_tree(scan_area, spec)
    children = sorted(
        entry.name for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)
    )
    if not children:
        return
    ignored = children[pick % len(children)]

    before = scan(root, depth_cap=depth_cap)
    (root / ".gitignore").write_text(f"{ignored}/\n", encoding="utf-8")
    after = scan(root, depth_cap=depth_cap)

    ignored_root = str(root / ignored)
    for candidate in after.candidates:
        assert candidate.path != ignored_root
        assert not candidate.path.startswith(ignored_root + os.sep)
    surviving = {candidate.path for candidate in after.candidates}
    assert surviving <= {candidate.path for candidate in before.candidates}
    assert scan(root, depth_cap=depth_cap) == after


@_SETTINGS
@given(spec=_TREES, depth_cap=_DEPTH_CAPS)
def test_candidates_stay_inside_the_scan_root(
    scan_area: Path,
    spec: _Dir,
    depth_cap: int,
) -> None:
    """Every candidate is a real directory strictly inside the scan root.

    Asserted after resolving links as well as before, which is what rules out the
    two ways out of the root: traversing a symlink, and honouring a declared
    member that points outside.
    """

    root, outside = _build_tree(scan_area, spec)
    resolved_root = os.path.realpath(root)

    tree = scan(root, depth_cap=depth_cap)

    assert tree.root == os.path.abspath(str(root))
    known = {candidate.path for candidate in tree.candidates}
    for candidate in tree.candidates:
        assert os.path.isabs(candidate.path)
        assert candidate.path.startswith(str(root) + os.sep)
        assert os.path.isdir(candidate.path)
        assert not os.path.islink(candidate.path)
        # The link a symlinked candidate would have been reached through cannot
        # appear in the path either, whether it pointed out of the root or back
        # at it.
        assert _LINK_NAME not in Path(candidate.path).relative_to(root).parts
        assert not os.path.realpath(candidate.path).startswith(os.path.realpath(outside))
        assert os.path.realpath(candidate.path).startswith(resolved_root + os.sep)
        assert candidate.name == os.path.basename(candidate.path)
        if candidate.parent_path is not None:
            # A parent that is not itself a candidate would be a dangling
            # ``parent_id`` once scaffolded.
            assert candidate.parent_path in known
            assert candidate.path.startswith(candidate.parent_path + os.sep)


@_SETTINGS
@given(spec=_TREES, depth_cap=_DEPTH_CAPS)
def test_pruned_directories_never_appear_in_a_candidate_path(
    scan_area: Path,
    spec: _Dir,
    depth_cap: int,
) -> None:
    """No candidate path passes through a pruned or hidden directory.

    Stated over the whole path rather than the candidate's own name, because
    pruning is only meaningful if it also excludes everything beneath: a
    vendored manifest is precisely the case where the signal is real and the
    candidate is still wrong. Declared members are covered by the same
    assertion — member expansion must not become a way around the rule.
    """

    root, _ = _build_tree(scan_area, spec)

    tree = scan(root, depth_cap=depth_cap)

    for candidate in tree.candidates:
        for segment in Path(candidate.path).relative_to(root).parts:
            assert segment not in PRUNE_DIRS
            # Every hidden directory is pruned, and the one exception (``.kiro``)
            # is a signal read from its parent, never a candidate itself.
            assert not segment.startswith(".")


@_SETTINGS
@given(spec=_TREES, depth_cap=_DEPTH_CAPS)
def test_every_tier_is_justified_by_what_is_on_disk(
    scan_area: Path,
    spec: _Dir,
    depth_cap: int,
) -> None:
    """Signals and tier are re-derived from the filesystem and must match.

    The tier decides what a preview ticks by default, so it is checked in both
    directions against an independent oracle: a repository or Kiro directory is
    auto-selected wherever it sits, a manifest is auto-selected only outside any
    package and offered inside one, and a candidate with no signal of its own
    exists only because a workspace declaration named it.
    """

    root, _ = _build_tree(scan_area, spec)
    manifest_names = _manifest_names()

    tree = scan(root, depth_cap=depth_cap)

    for candidate in tree.candidates:
        signals = set(candidate.signals)
        assert signals, f"{candidate.path} was offered with no reason recorded"
        on_disk = _own_signals(candidate.path, manifest_names)
        # ``member`` is the one signal that comes from a declaration file rather
        # than from the directory itself; everything else must be observable.
        assert signals - {SIGNAL_MEMBER} == on_disk
        assert len(candidate.signals) == len(signals)

        boundary = bool(on_disk & {SIGNAL_GIT, SIGNAL_KIRO})
        has_manifest = bool(on_disk - {SIGNAL_GIT, SIGNAL_KIRO})
        if boundary:
            assert candidate.tier is Tier.AUTO
        elif has_manifest:
            nested = _has_signalled_ancestor(candidate.path, root, manifest_names)
            assert candidate.tier is (Tier.OFFERED if nested else Tier.AUTO)
        else:
            assert candidate.tier is Tier.OFFERED
            assert SIGNAL_MEMBER in signals


@_SETTINGS
@given(spec=_TREES, depth_cap=_DEPTH_CAPS)
def test_no_candidate_lies_deeper_than_the_cap(
    scan_area: Path,
    spec: _Dir,
    depth_cap: int,
) -> None:
    """The depth cap bounds the whole result, member expansion included.

    Generated trees run deeper than the largest cap drawn, so the bound is
    exercised rather than merely satisfied — and members are expanded from
    patterns like ``**``, which is the one place a bound is easy to lose.
    """

    root, _ = _build_tree(scan_area, spec)

    tree = scan(root, depth_cap=depth_cap)

    for candidate in tree.candidates:
        depth = len(Path(candidate.path).relative_to(root).parts)
        assert 1 <= depth <= depth_cap


@_SETTINGS
@given(spec=_TREES, depth_cap=_DEPTH_CAPS)
def test_scanning_changes_nothing_on_disk(
    scan_area: Path,
    spec: _Dir,
    depth_cap: int,
) -> None:
    """A scan writes nothing, inside the scan root or beside it.

    Read-only is what makes a scan safe to point at an unfamiliar tree, and the
    snapshot spans the example's whole area: a scan that followed a link out of
    the root and wrote there would satisfy a root-only snapshot.
    """

    root, _ = _build_tree(scan_area, spec)
    area = root.parent
    before = _snapshot(area)

    tree = scan(root, depth_cap=depth_cap)

    assert isinstance(tree, CandidateTree)
    assert _snapshot(area) == before
