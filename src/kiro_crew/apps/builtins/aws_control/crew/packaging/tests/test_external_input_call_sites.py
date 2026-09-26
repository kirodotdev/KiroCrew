"""Every filesystem call in the builder states what its own failure means.

The builder's contract is that a run either produces a bundle or refuses with a
reason: ``main`` catches ``ExportRefused`` and prints it. Nothing else. So a
filesystem call whose failure is not caught somewhere leaves a raw traceback
instead of a stated reason, and it leaves it from OUTSIDE the refusal-keyed
cleanup, which is where a stranded staging tree and its marker come from -- and
that marker is what authorises the next run's recursive delete.

Six findings of that shape were reported one at a time, each with a different
symptom: a silent omission, a crash, a report claiming a success that did not
happen. A reviewer samples a diff, so six found by sampling implies more that
were not sampled. Counting them is the only way to know.

This module counts them. It walks the builder's syntax tree, finds every call
that touches the filesystem, and sorts each one into:

converted
    The call sits inside the body of a ``try`` whose handlers catch ``OSError``
    (or a subclass, or ``Exception``). A decision point exists: the module gets
    to say what the failure means.

unconverted
    It does not. The failure escapes to ``main`` as a traceback.

The gate is an equality, not a threshold: the unconverted sites this module
finds must match ``UNCONVERTED`` exactly. A new unconverted call fails the test
until its author either converts it or adds it here with a verdict, and a site
that gets converted fails the test until its entry is deleted. The inventory
shrinks as the sweep proceeds and can never silently grow.

A site's key is its enclosing function and the call it makes, never a line
number, so ordinary edits above it do not disturb the gate.

Two categories are tracked separately because they answer different questions.
``REPORT_WRITES`` pins the calls that publish a record, since a record written
before the operation it records can claim a success that never happened; that
ordering is checkable on its own, whatever the failures are typed as.
``PREDICATES`` pins the three-way distinction the sweep turns on: absence,
invalidity and unreadability are three different inputs and must not collapse
into one answer.
"""

from __future__ import annotations

import ast
import collections

from .test_producer import BUILD_PY

# ---------------------------------------------------------------------------
# what counts as touching the filesystem
# ---------------------------------------------------------------------------

# ``Path`` methods. Each one either performs a syscall or resolves a path, so
# each one can fail on operator- or author-supplied input.
PATH_METHODS = frozenset(
    {
        "chmod",
        "exists",
        "glob",
        "hardlink_to",
        "is_dir",
        "is_file",
        "is_symlink",
        "iterdir",
        "lchmod",
        "link_to",
        "lstat",
        "mkdir",
        "open",
        "read_bytes",
        "read_text",
        "readlink",
        "rename",
        "replace",
        "resolve",
        "rglob",
        "rmdir",
        "samefile",
        "stat",
        "symlink_to",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)

# ``os`` functions that take a PATH. A call that takes a descriptor instead --
# ``os.close``, ``os.fstat``, ``os.fchmod``, ``os.fsync`` -- is deliberately not
# here: the descriptor is one this module opened and already validated, so its
# failure is not a decision about operator- or author-supplied input, which is
# what this gate counts. The path-taking call that produced the descriptor IS
# counted, which is where the decision belongs.
OS_FUNCTIONS = frozenset(
    {
        "access",
        "chmod",
        "link",
        "listdir",
        "lstat",
        "makedirs",
        "mkdir",
        "open",
        "readlink",
        "remove",
        "rename",
        "replace",
        "rmdir",
        "scandir",
        "stat",
        "symlink",
        "unlink",
        "utime",
    }
)

OS_PATH_FUNCTIONS = frozenset({"exists", "getsize", "isdir", "isfile", "islink", "realpath"})

SHUTIL_FUNCTIONS = frozenset(
    {"copy", "copy2", "copyfile", "copymode", "copystat", "copytree", "move", "rmtree"}
)

# Modules whose attribute calls are never a filesystem touch, listed so a name
# collision with a ``Path`` method (``json.open`` does not exist, but
# ``re.match`` style collisions do appear as the set grows) cannot be counted.
NON_FILESYSTEM_MODULES = frozenset(
    {
        "ast",
        "base64",
        "collections",
        "hashlib",
        "json",
        "re",
        "subprocess",
        "sys",
        "textwrap",
        "time",
    }
)

# Exceptions that mean "this call's failure has a decision point here".
# ``RuntimeError`` is in the set because ``Path.resolve()`` raises it, not an
# ``OSError``, when it meets a symlink loop: a handler typed on ``OSError``
# alone does not catch that shape at all.
CONVERTING_EXCEPTIONS = frozenset(
    {
        "BaseException",
        "EnvironmentError",
        "Exception",
        "FileExistsError",
        "FileNotFoundError",
        "IOError",
        "IsADirectoryError",
        "NotADirectoryError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "ValueError",
    }
)

# Helpers that publish a record of what a run did. A call to one of these is a
# report write, wherever it appears.
REPORT_WRITERS = frozenset({"_publish_report", "write_plan"})

# The predicates that answer one value for three different inputs.
COLLAPSING_PREDICATES = frozenset({".exists", ".is_dir", ".is_file", ".is_symlink"})


def _call_label(node: ast.Call) -> str | None:
    """The call's stable name, or ``None`` when it does not touch the filesystem."""
    func = node.func
    if isinstance(func, ast.Name):
        return "open" if func.id == "open" else None
    if not isinstance(func, ast.Attribute):
        return None
    attr = func.attr
    base = func.value
    base_name = base.id if isinstance(base, ast.Name) else None
    if base_name == "os":
        return f"os.{attr}" if attr in OS_FUNCTIONS else None
    if base_name == "shutil":
        return f"shutil.{attr}" if attr in SHUTIL_FUNCTIONS else None
    if isinstance(base, ast.Attribute) and base.attr == "path":
        return f"os.path.{attr}" if attr in OS_PATH_FUNCTIONS else None
    if base_name in NON_FILESYSTEM_MODULES:
        return None
    return f".{attr}" if attr in PATH_METHODS else None


def _handler_converts(handlers: list[ast.ExceptHandler]) -> bool:
    for handler in handlers:
        caught = handler.type
        names: list[str] = []
        if isinstance(caught, ast.Name):
            names = [caught.id]
        elif isinstance(caught, ast.Tuple):
            names = [e.id for e in caught.elts if isinstance(e, ast.Name)]
        elif isinstance(caught, ast.Attribute):
            names = [caught.attr]
        elif caught is None:
            return True  # a bare ``except`` catches everything
        if any(name in CONVERTING_EXCEPTIONS for name in names):
            return True
    return False


class _Walk(ast.NodeVisitor):
    """Collects every filesystem call site with its enclosing function and cover."""

    def __init__(self) -> None:
        self.covered: list[bool] = []
        self.scope: list[str] = ["<module>"]
        self.sites: list[tuple[str, str, bool, int]] = []
        self.report_writes: list[tuple[str, str, int]] = []

    # A function defined inside a ``try`` body runs when it is CALLED, not where
    # it is written, so the enclosing ``try`` does not cover it. Each function
    # starts with an empty cover stack.
    def _function(self, node: ast.AST, name: str) -> None:
        outer, self.covered = self.covered, []
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()
        self.covered = outer

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._function(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._function(node, node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        self._function(node, f"{self.scope[-1]}.<lambda>")

    def _try(self, node: ast.Try | ast.TryStar) -> None:
        self.covered.append(_handler_converts(list(node.handlers)))
        for stmt in node.body:
            self.visit(stmt)
        self.covered.pop()
        # A handler body, an ``else`` and a ``finally`` all run OUTSIDE the
        # protection of their own ``try``, so they are walked uncovered.
        for handler in node.handlers:
            for stmt in handler.body:
                self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        self._try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:  # noqa: N802
        self._try(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Name) and func.id in REPORT_WRITERS:
            self.report_writes.append((self.scope[-1], func.id, node.lineno))
        label = _call_label(node)
        if label is not None:
            self.sites.append((self.scope[-1], label, any(self.covered), node.lineno))
        self.generic_visit(node)


def _walk() -> _Walk:
    walk = _Walk()
    walk.visit(ast.parse(BUILD_PY.read_text(encoding="utf-8")))
    return walk


def _unconverted() -> dict[str, dict[str, int]]:
    """``{function: {call: count}}`` for every site with no decision point."""
    found: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for scope, label, covered, _lineno in _walk().sites:
        if not covered:
            found[scope][label] += 1
    return {scope: dict(counter) for scope, counter in found.items()}


# ---------------------------------------------------------------------------
# the inventory
# ---------------------------------------------------------------------------

# Every filesystem call in the builder whose failure has no decision point, with
# the verdict for the function that holds it. A verdict opens with a category:
#
#   crash       the failure surfaces as a traceback instead of a stated reason
#   strand      as crash, and it can leave staging, its marker or a report temp
#               on disk -- the marker is what authorises the next run's delete
#   collapse    the call answers one value for absence and invalidity and raises
#               for unreadability, so a caller cannot tell the three apart
#   deliberate  the call is unconverted on purpose and the reason holds
#
# Converting a site means deleting its entry here. Adding a call without an entry
# fails ``test_unconverted_sites_match_the_inventory``.
#
# A caller that wraps the call is not on its own enough to clear an entry. The
# builder's transaction catches ``BaseException`` to restore state and re-raises,
# so a wrapped site can still reach the operator as a traceback: cleanup and a
# stated reason are separate properties and this gate tracks the reason.
UNCONVERTED: dict[str, dict[str, int]] = {
    "_copy_skill": {".is_dir": 1, ".is_file": 1, ".is_symlink": 2},
    "_descend": {"os.open": 1},
    "_dispose_via_private_aside": {".resolve": 1, "os.mkdir": 1},
    "_is_shape_this_build_never_writes": {".is_dir": 1, ".is_file": 1},
    "_open_dir_nofollow_pinned": {"os.open": 2},
    "_publish_report": {"os.open": 1, "os.unlink": 1},
    "_refuse_unless_our_report": {".is_file": 1},
    "_refuse_unless_this_build_wrote_it": {
        ".exists": 1,
        ".is_dir": 2,
        ".is_file": 2,
        ".is_symlink": 1,
    },
    "_refuse_unusable_parent": {".exists": 1, ".is_dir": 1},
    "_rmtree_pinned": {
        ".is_dir": 1,
        "os.open": 1,
        "os.rmdir": 1,
        "os.scandir": 1,
        "os.unlink": 1,
    },
    "_staged_tree_hash": {".is_dir": 1, ".is_file": 3, ".is_symlink": 1},
    "_tree_hash": {".is_file": 1, ".is_symlink": 1},
    "_write_bytes_nofollow": {".exists": 1, ".is_dir": 2, ".is_file": 1, ".write_bytes": 1},
    "_write_guarded": {".mkdir": 1},
    "build_bundle": {
        ".exists": 3,
        ".is_dir": 3,
        ".is_symlink": 1,
        ".resolve": 2,
    },
    "build_bundle.<lambda>": {"os.rename": 1},
    "skill_candidates": {".exists": 1, ".is_dir": 1, ".is_file": 3, ".is_symlink": 1},
    "write_plan": {".mkdir": 1},
}

VERDICTS: dict[str, str] = {
    "_copy_skill": (
        "collapse: the asset predicates answer False for an absent and an invalid entry and "
        "raise for an unreadable one; the caller's transaction removes staging but the "
        "operator is still given a traceback rather than a reason"
    ),
    "_descend": (
        "crash: the descriptor open on a tree member escapes, and neither the recursive "
        "self-call nor the captured-tree inspection converts it"
    ),
    "_dispose_via_private_aside": (
        "crash: resolve() raises RuntimeError on a symlink loop, which the callers' OSError "
        "handlers do not catch; the private mkdir escapes the same way"
    ),
    "_is_shape_this_build_never_writes": (
        "collapse: the shape predicate answers False for an absent and an invalid entry and "
        "raises for an unreadable one, and its answer gates a recursive delete"
    ),
    "_open_dir_nofollow_pinned": (
        "crash: the resolve() here is now normalised (a symlink loop's RuntimeError becomes "
        "OSError(ELOOP) that every caller's handler catches), but the anchor open and the "
        "Windows-fallback open still escape as a traceback"
    ),
    "_publish_report": (
        "crash: the report open and the temp unlink escape; the caller restores the prior "
        "report but names no reason for the failure"
    ),
    "_refuse_unless_our_report": (
        "collapse: is_file() answers False for an absent and an invalid report and raises for "
        "an unreadable one, and the build command converts neither"
    ),
    "_refuse_unless_this_build_wrote_it": (
        "collapse: exists() and is_dir()/is_file() answer False for an absent and an invalid "
        "entry and raise for an unreadable one; its sole caller in build_bundle now runs it "
        "inside the OSError-to-ExportRefused boundary that releases this run's staging and "
        "marker, so a raised OSError no longer strands them"
    ),
    "_refuse_unusable_parent": (
        "collapse: exists() and is_dir() answer False for an absent and an invalid parent and "
        "raise for an unreadable one; none of the three callers converts either"
    ),
    "_rmtree_pinned": (
        "crash: the recursive delete's own descriptor and unlink calls escape, and its "
        "recursive self-call is the caller that does not convert them"
    ),
    "_staged_tree_hash": (
        "collapse: the member predicates answer False for an absent and an invalid member and "
        "raise for an unreadable one, so a member that cannot be scanned is never named"
    ),
    "_tree_hash": (
        "collapse: the same member predicates as the staged hash, reached from skill "
        "enumeration, whose caller converts nothing"
    ),
    "_write_bytes_nofollow": (
        "crash: the parent predicates and the fallback write escape; the plain no-follow "
        "write calls it without converting them"
    ),
    "_write_guarded": (
        "crash: mkdir on the destination parent escapes, and the skill copy calls it without "
        "converting it"
    ),
    "build_bundle": (
        "crash: the post-marker report/plan reads now sit inside an OSError-to-ExportRefused "
        "boundary that releases this run's staging and marker, so those no longer strand; the "
        "remaining sites are the pre-staging shape probes and the parent resolves, whose "
        "failure is a traceback before anything is created"
    ),
    "build_bundle.<lambda>": (
        "deliberate: the promotion rename runs inside the transaction that restores the prior "
        "bundle; a lambda is a separate scope to this walk, not a separate failure path"
    ),
    "skill_candidates": (
        "collapse: the root and asset predicates answer False for an absent and an invalid "
        "path and raise for an unreadable one, so a selected asset can be dropped in silence"
    ),
    "write_plan": (
        "crash: mkdir on the plan's parent escapes, so a plan run ends with a traceback "
        "instead of a reason"
    ),
}

CATEGORIES = ("crash:", "strand:", "collapse:", "deliberate:")

# The calls that publish a record of a run, with where each one sits relative to
# the operation it records. A record written before its operation can claim a
# success that never happened, so the ordering is pinned on its own.
REPORT_WRITES: dict[tuple[str, str], str] = {
    ("build_bundle", "_publish_report"): (
        "downstream: the rename that promotes the bundle returns first and sets the promoted "
        "flag, and only then is the report published"
    ),
    ("_cmd_plan", "write_plan"): (
        "downstream: the command reports what the write itself returned, so there is no "
        "assumed outcome to record"
    ),
}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_the_walk_finds_the_calls_it_is_meant_to_find() -> None:
    """The walk is load-bearing, so prove it sees a populated builder.

    Every assertion below compares against what the walk found. A classifier that
    silently matched nothing would satisfy all of them, so this floor comes
    first: if it fails, the harness is wrong and no other failure here means
    what it says.
    """
    walk = _walk()
    assert len(walk.sites) > 100, "the walk found almost no filesystem calls; suspect the walk"
    converted = [site for site in walk.sites if site[2]]
    assert converted, "the walk found no converted call at all; suspect _handler_converts"
    scopes = {site[0] for site in walk.sites}
    for expected in ("build_bundle", "skill_candidates", "_publish_report"):
        assert expected in scopes, f"the walk did not enter {expected}"
    labels = {site[1] for site in walk.sites}
    assert COLLAPSING_PREDICATES & labels, "the walk found none of the collapsing predicates"


def test_unconverted_sites_match_the_inventory() -> None:
    """No new unconverted call, and no entry left behind by a converted one."""
    found = _unconverted()
    appeared = {
        f"{scope}.{label}": count
        for scope, calls in found.items()
        for label, count in calls.items()
        if UNCONVERTED.get(scope, {}).get(label, 0) != count
    }
    vanished = {
        f"{scope}.{label}": count
        for scope, calls in UNCONVERTED.items()
        for label, count in calls.items()
        if found.get(scope, {}).get(label, 0) != count
    }
    assert not appeared, (
        "these filesystem calls have no decision point and are not in the inventory: "
        f"{sorted(appeared)}. Either convert the failure into ExportRefused, or add the site "
        "to UNCONVERTED with a verdict saying what its failure currently means."
    )
    assert not vanished, (
        "the inventory claims these sites are unconverted and they are not: "
        f"{sorted(vanished)}. Delete the stale entries so the inventory keeps shrinking."
    )


def test_every_inventory_entry_states_a_verdict() -> None:
    """An entry without a categorised verdict is a number nobody has judged."""
    assert set(UNCONVERTED) == set(VERDICTS), (
        "every inventoried function needs a verdict and every verdict needs a function; "
        f"only in UNCONVERTED: {sorted(set(UNCONVERTED) - set(VERDICTS))}; "
        f"only in VERDICTS: {sorted(set(VERDICTS) - set(UNCONVERTED))}"
    )
    for scope, verdict in VERDICTS.items():
        assert verdict.startswith(CATEGORIES), (
            f"the verdict for {scope} opens with none of {CATEGORIES}; the category is what "
            "tells the sweep whether the site strands state or only crashes"
        )
        assert len(verdict) > 40, f"the verdict for {scope} is too short to say anything"


def test_report_writes_state_their_ordering() -> None:
    """A record may only be written downstream of the result it records."""
    found = {(scope, writer) for scope, writer, _lineno in _walk().report_writes}
    assert found == set(REPORT_WRITES), (
        "a call that publishes a record is not pinned with its ordering: "
        f"unpinned {sorted(found - set(REPORT_WRITES))}, "
        f"pinned but absent {sorted(set(REPORT_WRITES) - found)}. State whether the write is "
        "downstream of the operation it records."
    )
    for site, ordering in REPORT_WRITES.items():
        assert ordering.startswith("downstream:"), (
            f"the record written at {site} is not downstream of its operation, so it can "
            "claim a success that did not happen"
        )
