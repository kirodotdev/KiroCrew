"""``state.json`` stays a WHOLE-FILE rewrite, and this gate is that policy's teeth.

THE POLICY. ``state.json`` is a run's artifact and evidence record, not a
coordination primitive. Scheduling's source of truth is the durable task queue,
which carries its own generation fencing. So the write model for ``state.json``
stays the whole-file rewrite it already is: no revision counter, no
compare-and-swap retry loop, no per-field or append-merge format. A second
coordination protocol one layer below the one that already exists buys ordering
this file does not need, and costs a format every reader must agree on -- the
state reader, the tombstone pruner, the keep scan, orphan recovery and the
legacy-record migration.

WHAT KEEPS THE WHOLE-FILE REWRITE SAFE. One invariant:

    Every whole-file ``state.json`` write happens at a KNOWN site, and each site
    that can run on the event loop carries its own fence.

The rewrite is a read, then a blocking fsync-and-rename, then nothing -- so two
writers on one agent interleave, and the later one restores a snapshot that
predates the other's write. Losing the other writer's fields is the visible
half; rolling back fields NEITHER writer touched is the damaging half. Off-loop
callers are serialized by the per-agent lock, which closes the class for them.
On the loop that lock cannot be waited on, because parking the gateway's only
loop behind a pool thread's fsync is the blocking-call class the repo forbids.
So an on-loop site carries its own fence instead, and a new site with none is
the defect this gate refuses to let land.

THE ASYMMETRY CLOSES BY MOVING WRITERS OFF THE LOOP, not by changing the file
format. A writer that leaves the loop needs no fence of its own: it inherits the
lock. That is the direction of travel, which is why the census below should
shrink and never grow.

WHY THE CENSUS IS THE SITES AND NOT "IS IT ON THE LOOP". Every write site today
sits in a synchronous function that a coroutine calls, so no amount of
``async def`` inspection can see it: asking "is this on the loop" is not
statically answerable, while "is this a write site" is exactly answerable. So
rule one pins the site set with its per-site call count, and a new write
anywhere fails -- forcing its author to say which fence it carries and whether
it runs on the loop. Rule two is the sharper case that IS statically decidable:
a write sitting DIRECTLY in a coroutine body. There are none, and that is a
measurement, not a wish, so rule two has no allowlist at all.

SCOPE BOUNDARY. ``asyncio.to_thread(update_state, ...)`` passes the writer as a
bare Name rather than calling it, so the sanctioned off-loop helper is not a site
here; that route has its own gate, which keeps every off-loop writer on the
drained helper.

ONE RECORDING PLACE. An exception is recorded in the census and nowhere else, so
the fence and the site sit together where the policy lives. The detector matches a
writer by NAME, so an unrelated object carrying a same-named method reads as a
site; the remedy there is to rename that symbol or record it, not to add a second
way of spelling an exception.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import PurePath, PurePosixPath, PureWindowsPath

import pytest
from source_corpus import parsed_candidates, src_root

#: Names whose call performs, or delegates, a read / merge / whole-file rewrite.
#: ``state_writer`` is retention promotion's injected seam, whose default IS the
#: whole-file writer, so the call through it is a write site like any other.
_WRITER_NAMES = frozenset({"update_state", "update_execution_context", "state_writer"})

#: The low-level writer. A caller that bypasses the helpers still lands the same
#: whole-file rewrite, so it is a site on the same terms.
_ATOMIC_WRITER = "_atomic_write"
_STATE_FILE = "state.json"

#: A nested callable is a separate execution frame, so it does not inherit the
#: enclosing function's identity; it is scanned on its own by the top-level walk.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

#: THE CENSUS. Every whole-file ``state.json`` write site, with how many calls it
#: holds and the fence that makes it safe. Keyed by ``(module, function, call)``
#: and pinned by COUNT, so a second write added inside an already-listed function
#: fails too.
#:
#: This list is expected to SHRINK. Moving a site off the loop lets it inherit
#: the per-agent lock; a new entry is a new path into a file three fences already
#: guard, and calls for the policy above to be revisited rather than one more
#: line here.
#:
#: The fence text is prose, and this gate does not re-derive it. A site that
#: moves on or off the loop keeps the same key, so the commit that moves it
#: updates or deletes its row here.
_WRITE_SITES: dict[tuple[str, str, str], tuple[int, str]] = {
    (
        "kiro_crew/execution_context.py",
        "bind_session_execution",
        "update_execution_context",
    ): (1, "off loop at every call site; serialized by the callee's unconditional lock"),
    (
        "kiro_crew/subagent_manager/continuation.py",
        "release_conversation_impl",
        "update_state",
    ): (1, "on loop; fenced by the release path refusing while the run is in flight"),
    (
        "kiro_crew/subagent_persistence.py",
        "create_agent_folder",
        "update_execution_context",
    ): (1, "on loop; the one acquire that can wait, bounded by the spawn and admission path"),
    (
        "kiro_crew/subagent_persistence.py",
        "create_agent_folder",
        "_atomic_write(state.json)",
    ): (1, "creation path; writes the initial file before any writer can race"),
    (
        "kiro_crew/subagent_persistence.py",
        "promote_retention",
        "state_writer",
    ): (1, "on loop; fenced by a non-blocking probe of the state lock, RETRYABLE on contention"),
    (
        "kiro_crew/subagent_persistence.py",
        "tighten_run_memory_mode",
        "update_execution_context",
    ): (1, "off loop through to_thread; serialized by the callee's unconditional lock"),
    (
        "kiro_crew/subagent_persistence.py",
        "update_execution_context",
        "_atomic_write(state.json)",
    ): (2, "the writer's own rewrite, inside the lock it holds for read and write"),
    (
        "kiro_crew/subagent_persistence.py",
        "update_state",
        "_atomic_write(state.json)",
    ): (
        1,
        "the writer's own rewrite; off-loop callers hold the lock, on-loop sites are listed here",
    ),
    (
        "kiro_crew/subagent_persistence.py",
        "write_run_agent",
        "update_execution_context",
    ): (1, "off loop pre-run write; serialized by the callee's unconditional lock"),
}


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local names bound to a writer back to the real name.

    ``from kiro_crew.subagent_persistence import update_state as write`` must
    still resolve, or one aliased import silently defeats the gate.
    """
    aliases: dict[str, str] = {name: name for name in _WRITER_NAMES}
    aliases[_ATOMIC_WRITER] = _ATOMIC_WRITER
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name in _WRITER_NAMES or alias.name == _ATOMIC_WRITER:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _names_state_file(node: ast.AST) -> bool:
    """True when the subtree spells the state filename as a literal."""
    return any(isinstance(sub, ast.Constant) and sub.value == _STATE_FILE for sub in ast.walk(node))


def _call_label(
    call: ast.Call,
    aliases: dict[str, str],
    scope: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> str | None:
    """Return the write-site label for *call*, or None when it is not one.

    An attribute call (``persistence.update_state(...)``) matches on the
    attribute: the object it hangs off is not statically knowable and the same
    whole-file rewrite is reached either way.

    For ``_atomic_write`` the filename is usually built a statement earlier
    (``p = dir / "state.json"`` then ``_atomic_write(p, state)``), so the literal
    counts anywhere in the ENCLOSING function, not only in the call itself.
    """
    fn = call.func
    if isinstance(fn, ast.Name):
        name = aliases.get(fn.id)
    elif isinstance(fn, ast.Attribute):
        name = fn.attr if (fn.attr in _WRITER_NAMES or fn.attr == _ATOMIC_WRITER) else None
    else:
        return None
    if name is None:
        return None
    if name != _ATOMIC_WRITER:
        return name
    if _names_state_file(call) or (scope is not None and _names_state_file(scope)):
        return f"{_ATOMIC_WRITER}({_STATE_FILE})"
    return None


class _SiteScan(ast.NodeVisitor):
    """Collect write sites with their nearest enclosing function and its kind."""

    def __init__(self, path: str, aliases: dict[str, str]) -> None:
        self.path = path
        self.aliases = aliases
        self.scopes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.names: list[str] = []
        self.on_loop: list[bool] = []
        #: ``(path, function, label, in_coroutine_body, lineno)``
        self.hits: list[tuple[str, str, str, bool, int]] = []

    def _enter(self, node, is_async: bool) -> None:  # type: ignore[no-untyped-def]
        self.scopes.append(node)
        self.names.append(node.name)
        self.on_loop.append(is_async)
        self.generic_visit(node)
        self.scopes.pop()
        self.names.pop()
        self.on_loop.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, True)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # A lambda is its own frame and carries no name to key a census entry on;
        # keep the stack honest so a write inside one is not attributed outward.
        self.names.append("<lambda>")
        self.on_loop.append(False)
        self.generic_visit(node)
        self.names.pop()
        self.on_loop.pop()

    def visit_Call(self, node: ast.Call) -> None:
        scope = self.scopes[-1] if self.scopes else None
        label = _call_label(node, self.aliases, scope)
        if label is not None:
            self.hits.append(
                (
                    self.path,
                    self.names[-1] if self.names else "<module>",
                    label,
                    bool(self.on_loop and self.on_loop[-1]),
                    node.lineno,
                )
            )
        self.generic_visit(node)


def find_write_sites(
    source: str,
    path: str = "<memory>",
    tree: ast.Module | None = None,
) -> list[tuple[str, str, str, bool, int]]:
    """Return ``(path, function, label, in_coroutine_body, lineno)`` per write site.

    *tree* lets a caller that already parsed *source* hand the tree over; it must
    be the parse of *source*.
    """
    if tree is None:
        tree = ast.parse(source)
    scan = _SiteScan(path, _import_aliases(tree))
    scan.visit(tree)
    return scan.hits


#: Every tracked name is spelled literally at a call site or resolved from a
#: ``from``-import in the same module, and ``_atomic_write`` needs the state
#: filename in scope. A module mentioning none of that holds no site and is not
#: worth parsing.
_REQUIRE_ANY = tuple(sorted(_WRITER_NAMES | {_ATOMIC_WRITER}))


def _module_key(path: PurePath, base: PurePath) -> str:
    """The module's path relative to *base*, POSIX-separated.

    ``str()`` on a Windows path yields backslashes, so a census keyed on the
    forward-slash spelling would miss every entry and fail this gate on Windows
    alone. Normalising the separator keeps one census valid on every platform.
    """
    try:
        rel: PurePath = path.relative_to(base)
    except ValueError:
        rel = path
    return rel.as_posix()


def collect_repo_write_sites() -> list[tuple[str, str, str, bool, int]]:
    """Scan every ``kiro_crew/**/*.py`` for whole-file ``state.json`` write sites."""
    base = src_root().parent
    out: list[tuple[str, str, str, bool, int]] = []
    for py, text, tree in parsed_candidates(require_any=_REQUIRE_ANY):
        out.extend(find_write_sites(text, _module_key(py, base), tree=tree))
    return out


def _census_counts(
    sites: list[tuple[str, str, str, bool, int]],
) -> Counter[tuple[str, str, str]]:
    return Counter((path, func, label) for path, func, label, _async, _line in sites)


_REMEDY = (
    "state.json is a whole-file rewrite by recorded policy, so a writer that "
    "lands late restores a snapshot predating another writer and rolls back "
    "fields neither writer touched. Off-loop callers are serialized by the "
    "per-agent lock; an on-loop caller cannot wait on it and needs its own "
    "fence. Move the write off the loop (it then inherits the lock), or reach "
    "it through promote_retention, which probes the lock non-blocking and "
    "reports a retryable result."
)


# --------------------------------------------------------------------------- #
# The gate                                                                     #
# --------------------------------------------------------------------------- #
def test_no_new_whole_file_state_write_site() -> None:
    sites = collect_repo_write_sites()
    live = _census_counts(sites)
    lines: dict[tuple[str, str, str], list[int]] = {}
    for path, func, label, _async, lineno in sites:
        lines.setdefault((path, func, label), []).append(lineno)

    problems: list[str] = []
    for key, count in sorted(live.items()):
        expected = _WRITE_SITES.get(key)
        path, func, label = key
        where = f"{path}:{sorted(lines[key])} {label}(...) in {func}(...)"
        if expected is None:
            problems.append(f"  NEW SITE     {where}")
        elif expected[0] != count:
            problems.append(f"  COUNT {expected[0]} -> {count}  {where}")
    if problems:
        raise AssertionError(
            "Unrecorded whole-file `state.json` write site(s).\n"
            f"{_REMEDY}\n"
            "If the write is genuinely fenced, add it to _WRITE_SITES with its "
            "count and the fence named.\n" + "\n".join(problems)
        )


def test_no_state_write_directly_in_a_coroutine_body() -> None:
    """No allowlist here, because the measured answer today is zero.

    Every write site lives in a synchronous function, so a write appearing
    directly in a coroutine body is new by construction -- and it is the one
    form whose on-loop status is not in doubt.
    """
    on_loop = [
        f"  {path}:{lineno}  {label}(...) in async {func}(...)"
        for path, func, label, in_coroutine, lineno in collect_repo_write_sites()
        if in_coroutine
    ]
    assert not on_loop, (
        "Whole-file `state.json` write inside an `async def` body.\n"
        + _REMEDY
        + "\n"
        + "\n".join(on_loop)
    )


def test_every_census_entry_still_exists() -> None:
    """The census must not outlive the sites it records.

    An entry for a call that moved or was deleted records a fence nobody relies
    on, and it silently licenses the day a new write lands in a module and
    function of the same name. So a stale entry fails here rather than waiting.
    """
    live = set(_census_counts(collect_repo_write_sites()))
    stale = sorted(key for key in _WRITE_SITES if key not in live)
    assert not stale, (
        "_WRITE_SITES records site(s) that no longer exist -- remove them so the "
        f"census keeps matching the code: {stale}"
    )


# --------------------------------------------------------------------------- #
# Meta-tests: prove each gate above still discriminates                        #
# --------------------------------------------------------------------------- #
#: A gate whose own assertion is dropped passes silently, and the detector
#: meta-tests further down cannot see that: they exercise the scanner, never a
#: gate's verdict. So each gate is driven here against a census or a site list
#: that MUST fail it. Dropping an assertion above reddens one of these.
_PINNED_SITE = (
    "kiro_crew/subagent_persistence.py",
    "promote_retention",
    "state_writer",
)


def _patch_census(
    monkeypatch: pytest.MonkeyPatch,
    census: dict[tuple[str, str, str], tuple[int, str]],
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_WRITE_SITES", census)


def test_the_census_gate_fails_on_an_unrecorded_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _PINNED_SITE in _WRITE_SITES, "the pinned key left the census"
    _patch_census(monkeypatch, {k: v for k, v in _WRITE_SITES.items() if k != _PINNED_SITE})
    with pytest.raises(AssertionError, match="NEW SITE"):
        test_no_new_whole_file_state_write_site()


def test_the_census_gate_fails_when_a_recorded_count_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count, fence = _WRITE_SITES[_PINNED_SITE]
    _patch_census(monkeypatch, {**_WRITE_SITES, _PINNED_SITE: (count + 1, fence)})
    with pytest.raises(AssertionError, match="COUNT"):
        test_no_new_whole_file_state_write_site()


def test_the_stale_entry_gate_fails_on_a_phantom_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phantom = ("kiro_crew/nowhere.py", "gone", "update_state")
    _patch_census(monkeypatch, {**_WRITE_SITES, phantom: (1, "records nothing live")})
    with pytest.raises(AssertionError, match="nowhere.py"):
        test_every_census_entry_still_exists()


def test_the_coroutine_gate_fails_on_a_write_in_a_coroutine_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys.modules[__name__],
        "collect_repo_write_sites",
        lambda: [("kiro_crew/x.py", "run", "update_state", True, 7)],
    )
    with pytest.raises(AssertionError, match="async def"):
        test_no_state_write_directly_in_a_coroutine_body()


# --------------------------------------------------------------------------- #
# Meta-tests: prove the detector finds and ignores the right things             #
# --------------------------------------------------------------------------- #
def _labels(source: str) -> list[str]:
    return [label for _p, _f, label, _a, _l in find_write_sites(source)]


def test_finds_a_direct_writer_call() -> None:
    src = "def f(a):\n    update_state(a, keep=False)\n"
    assert _labels(src) == ["update_state"]


def test_finds_a_writer_call_in_a_sync_helper_and_marks_it_off_body() -> None:
    src = "def f(a):\n    update_state(a, keep=False)\n"
    assert [(v[1], v[3]) for v in find_write_sites(src)] == [("f", False)]


def test_marks_a_writer_call_in_a_coroutine_body() -> None:
    src = "async def f(a):\n    update_state(a, keep=False)\n"
    assert [(v[1], v[3]) for v in find_write_sites(src)] == [("f", True)]


def test_finds_an_attribute_spelled_call() -> None:
    src = "def f(a, x):\n    self._persist.update_execution_context(a, x)\n"
    assert _labels(src) == ["update_execution_context"]


def test_finds_an_aliased_import() -> None:
    src = (
        "from kiro_crew.subagent_persistence import update_state as write\n"
        "def f(a):\n"
        "    write(a, keep=True)\n"
    )
    assert _labels(src) == ["update_state"]


def test_finds_the_injected_writer_seam() -> None:
    src = "def f(a, state_writer):\n    state_writer(a, keep=True)\n"
    assert _labels(src) == ["state_writer"]


def test_finds_atomic_write_naming_the_state_file_inline() -> None:
    src = 'def f(d, s):\n    _atomic_write(d / "state.json", s)\n'
    assert _labels(src) == ["_atomic_write(state.json)"]


def test_finds_atomic_write_whose_path_is_built_a_statement_earlier() -> None:
    src = 'def f(d, s):\n    p = d / "state.json"\n    _atomic_write(p, s)\n'
    assert _labels(src) == ["_atomic_write(state.json)"]


def test_ignores_atomic_write_of_another_file() -> None:
    src = 'def f(d, s):\n    _atomic_write(d / "tombstone.json", s)\n'
    assert find_write_sites(src) == []


def test_ignores_the_off_loop_helper_form() -> None:
    # The writer is passed as a bare Name, so it runs in a pool thread under the
    # per-agent lock. That route has its own gate.
    src = "import asyncio\nasync def f(a):\n    await asyncio.to_thread(update_state, a, turns=1)\n"
    assert find_write_sites(src) == []


def test_ignores_a_writer_passed_as_a_keyword_argument() -> None:
    src = "def f(a, p):\n    p.promote_retention(a, state_writer=update_state)\n"
    assert find_write_sites(src) == []


def test_attributes_a_write_in_a_nested_function_to_that_function() -> None:
    src = "async def outer(a):\n    def _write():\n        update_state(a, keep=False)\n    return _write\n"
    assert [(v[1], v[3]) for v in find_write_sites(src)] == [("_write", False)]


def test_module_key_normalises_a_windows_separator() -> None:
    # The census is keyed on forward slashes, so a backslash spelling would miss
    # every entry and red this gate on one platform only.
    base = PureWindowsPath(r"C:\repo\src")
    path = PureWindowsPath(r"C:\repo\src\kiro_crew\subagent_manager\continuation.py")
    assert _module_key(path, base) == "kiro_crew/subagent_manager/continuation.py"


def test_module_key_keeps_a_posix_path_unchanged() -> None:
    base = PurePosixPath("/repo/src")
    path = PurePosixPath("/repo/src/kiro_crew/subagent_persistence.py")
    assert _module_key(path, base) == "kiro_crew/subagent_persistence.py"


def test_module_key_falls_back_to_the_whole_path_off_base() -> None:
    base = PurePosixPath("/repo/src")
    path = PurePosixPath("/elsewhere/kiro_crew/x.py")
    assert _module_key(path, base) == "/elsewhere/kiro_crew/x.py"


def test_an_unrelated_call_is_not_a_site() -> None:
    src = "def f(a):\n    a.refresh_state()\n    a.save()\n"
    assert find_write_sites(src) == []
