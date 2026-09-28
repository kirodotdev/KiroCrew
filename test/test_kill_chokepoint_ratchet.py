"""Ratchet: how many places can still end a runtime without saying who fired.

Every path that signals a process reaches one of a handful of primitives in
:mod:`kiro_crew.platform_compat`, and a caller that reaches them directly does so
unattributed: the log that follows says a process died, never who decided it
should. Routing those callers through one gated place
(:func:`kiro_crew.runtime_ownership.authorize_runtime_kill`, which both refuses a
leased runtime and writes the attribution) is the work of several changes, so this module measures the remainder and ratchets it:
:data:`BYPASS_BASELINE` may go DOWN in any change and may never go up. A new kill
site must either attribute itself or lower something else to pay for itself.

Three things are pinned, and the third is what makes the first two mean anything:

* the COUNT of direct primitive calls outside the primitive module
  (:func:`test_kill_primitive_bypass_count_does_not_grow`);
* that the count is not low merely because the scan matches nothing -- a real
  primitive call is found where one is known to be
  (:func:`test_the_needles_match_a_known_call_site`);
* that the two paths every other kill funnels into DO attribute themselves
  (:func:`test_attributed_kill_paths_consult_the_gate`). Without this the count
  could fall to zero while nothing was ever logged.

Complements ``test_process_identity_structural.py`` rather than repeating it.
That module answers a different question -- whether the reaper's four modules
address a process by a verified handle instead of a bare pid, which is about
killing a STRANGER that inherited a recycled pid -- and it forbids outright inside
those modules. This one counts, repo-wide.

The scan is :mod:`ast`, so only real call expressions count: a primitive named in
a docstring or a comment is not a kill site, and a grep-based version of this test
reported 108 where 66 calls exist. The needles are assembled from fragments so
this file does not contain the names it searches for, and so a reader grepping the
tree for kill sites is not handed the detector as one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: The module that DEFINES the primitives. Its own calls are the primitives'
#: implementation -- ``kill_pid_pinned`` delegating to ``kill_pid``, which issues
#: the group signal -- not a caller deciding a process should die.
PRIMITIVE_HOME = "platform_compat.py"

#: Assembled, not written: this file must not contain the names it scans for.
_KILL = "kill"
PID_KILL_PRIMITIVES = frozenset(
    {
        _KILL + "_pid",
        _KILL + "_process_tree",
        _KILL + "_pid_pinned",
        _KILL + "_process_tree_pinned",
        _KILL + "pg",
    }
)

#: Direct primitive calls outside :data:`PRIMITIVE_HOME`, measured on the change
#: that introduced the attribution line. RATCHET: lower this when a change routes
#: a site through an attributed path; never raise it for a site of your own.
#:
#: What the remainder is, so the next change knows where to look: the session
#: teardown's own escalation (``session_pid``), app-backend and dev-preview
#: process management, the test harness, the cron script runner, the PDF
#: extractor's Windows timeout path, and a spread of single-site tools. The two
#: biggest runtime kill paths already consult the gate -- see
#: :func:`test_attributed_kill_paths_consult_the_gate` -- and their primitive calls
#: remain counted here, because the gate brackets their escalation rather than
#: replacing it.
#:
#: The one legitimate reason to raise this is a site the BASE branch gained, which
#: this number tracks and does not govern: the PDF extractor's Windows-only tree
#: kill is such a site, and it kills a document-parsing subprocess that owns no
#: session runtime, so an ownership gate there would consult a table that can
#: never hold a lease for it. Raising it for a site the change itself adds is what
#: the ratchet exists to stop.
BYPASS_BASELINE = 67

#: Functions that must say who fired before they signal anything, as module path
#: -> function name. These are the paths every other kill funnels into: the
#: synchronous provider teardown (a dashboard reset-all, a pool teardown, a
#: failed start's cleanup) and the runtime's own kill.
ATTRIBUTED_KILL_PATHS = (
    ("session_pid.py", "_sync_kill_provider"),
    ("acp/runtime.py", "kill"),
)

GATE = "authorize_runtime_kill"

#: A call site the needles MUST find, so an empty scan cannot read as success.
#: ``kill_process_tree`` is what a POSIX group teardown ends in.
KNOWN_CALL_SITE = ("session_pid.py", _KILL + "_process_tree")


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _primitive_calls(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (node.lineno, name)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (name := _callee_name(node)) in PID_KILL_PRIMITIVES
    ]


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _bypass_sites() -> dict[str, list[tuple[int, str]]]:
    sites: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == PRIMITIVE_HOME:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        found = _primitive_calls(tree)
        if found:
            sites[str(path.relative_to(SRC))] = found
    return sites


def _function_named(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_kill_primitive_bypass_count_does_not_grow() -> None:
    """The number of sites that kill without saying who fired may only fall."""
    sites = _bypass_sites()
    total = sum(len(v) for v in sites.values())
    breakdown = "\n".join(
        f"  {count:3d}  {module}"
        for module, count in sorted(
            ((m, len(v)) for m, v in sites.items()), key=lambda kv: (-kv[1], kv[0])
        )
    )
    assert total <= BYPASS_BASELINE, (
        f"{total} direct kill-primitive call(s) now bypass the attribution point, above "
        f"the baseline of {BYPASS_BASELINE}. Route the new site through a path that calls "
        f"kiro_crew.runtime_ownership.{GATE} instead of raising this number.\n"
        f"Per module:\n{breakdown}"
    )


def test_bypass_baseline_is_not_stale() -> None:
    """A baseline left far above the real count stops ratcheting anything.

    Without this, a change that lowers 20 sites but leaves the number alone hands
    the next 20 regressions a free pass.
    """
    total = sum(len(v) for v in _bypass_sites().values())
    assert total == BYPASS_BASELINE, (
        f"the real count is {total} but BYPASS_BASELINE says {BYPASS_BASELINE}. "
        f"Lower the baseline to {total} in this change -- that is the ratchet clicking."
    )


def test_the_needles_match_a_known_call_site() -> None:
    """POSITIVE CONTROL: the scan finds a call that is known to be there.

    Without this, the counting tests pass trivially on a tree where the needles
    match nothing at all -- a renamed primitive, a typo in a fragment -- and that
    reads as total success.
    """
    module, needle = KNOWN_CALL_SITE
    tree = _parse(SRC / module)
    assert tree is not None, f"{module} must parse"
    found = [name for _, name in _primitive_calls(tree)]
    assert needle in found, (
        f"{module} calls no {needle}, so the needles in this file match nothing "
        f"and every count above is meaningless"
    )


@pytest.mark.parametrize(("module", "function"), ATTRIBUTED_KILL_PATHS)
def test_attributed_kill_paths_consult_the_gate(module: str, function: str) -> None:
    """The two paths every other kill funnels into must say who fired.

    This is what a falling count has to mean. A count that reached zero because
    the primitives were renamed, with nothing ever logged, would satisfy the
    ceiling above and leave the field exactly as undiagnosable as before.
    """
    tree = _parse(SRC / module)
    assert tree is not None, f"{module} must parse"
    target = _function_named(tree, function)
    assert target is not None, f"{module} must define {function}"
    notes = [
        node
        for node in ast.walk(target)
        if isinstance(node, ast.Call) and _callee_name(node) == GATE
    ]
    assert notes, (
        f"{module}:{function} signals a process without calling {GATE}, so a "
        f"runtime it ends leaves nothing in the log naming the caller or the reason"
    )


@pytest.mark.parametrize(("module", "function"), ATTRIBUTED_KILL_PATHS)
def test_the_gate_call_names_a_caller_and_a_reason(module: str, function: str) -> None:
    """A bare call that passes neither would log an empty attribution."""
    tree = _parse(SRC / module)
    assert tree is not None
    target = _function_named(tree, function)
    assert target is not None
    for node in ast.walk(target):
        if isinstance(node, ast.Call) and _callee_name(node) == GATE:
            passed = {kw.arg for kw in node.keywords}
            assert {"reason", "caller"} <= passed, (
                f"{module}:{function} calls {GATE} without both reason and "
                f"caller (passed: {sorted(p for p in passed if p)})"
            )
            return
    pytest.fail(f"{module}:{function} does not call {GATE}")


@pytest.mark.parametrize(("module", "function"), ATTRIBUTED_KILL_PATHS)
def test_the_gate_verdict_is_acted_on(module: str, function: str) -> None:
    """Calling the gate is not gating on it -- the refusal must stop the kill.

    The weaker sibling above is satisfied by a site that calls the gate and drops
    the answer on the floor, which logs the refusal and then signals the process
    anyway. That failure is invisible in a log review: the REFUSED line is
    written, so the gate looks like it held while every kill still went through.

    So require the call to sit under ``if not <gate>(...)`` whose body leaves the
    function. Structural rather than behavioural because the alternative is
    booting a runtime per path; the mutation that deletes a release site covers
    the behaviour, and this covers the shape that mutation assumes.
    """
    tree = _parse(SRC / module)
    assert tree is not None
    target = _function_named(tree, function)
    assert target is not None

    def _guards(node: ast.AST) -> bool:
        """An ``if not GATE(...)`` whose body returns or raises."""
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            return False
        if not (isinstance(test.operand, ast.Call) and _callee_name(test.operand) == GATE):
            return False
        return any(isinstance(leaf, (ast.Return, ast.Raise)) for leaf in ast.walk(node))

    assert any(_guards(node) for node in ast.walk(target)), (
        f"{module}:{function} calls {GATE} without acting on the verdict: a refusal "
        f"must leave the function before anything is signalled, or the gate only "
        f"logs while every kill still proceeds"
    )


# -- release-before-kill at the post-registration kill sites --

#: Each entry is a file plus the release call that must precede a kill in the
#: SAME block. Only the post-registration sites appear: every other hard-kill
#: site in the tree runs before a session is registered, holds no lease, and is
#: authorized without releasing anything. Adding a release there would be a call
#: with no effect, which is why this list is short rather than exhaustive.
_RELEASE_BEFORE_KILL = (
    (
        "src/kiro_crew/session_allocation.py",
        "release_session_lease",
        "_dispatch_hard_kill",
    ),
    (
        "src/kiro_crew/dashboard/handlers/sessions.py",
        "release_session_lease",
        "_sync_kill_provider",
    ),
    (
        "src/kiro_crew/acp/session_provider.py",
        "release_runtime_lease",
        "kill",
    ),
    # The mint's child is defended by a TENANCY rather than a lease -- it is a
    # Connect flow's process, not a session's -- but the ordering rule is the
    # same one: the gate refuses a claimed process, so a claim still held when
    # this flow tears its own child down makes it refuse its own teardown.
    (
        "src/kiro_crew/connections/mint.py",
        "release_runtime_tenancy",
        "_shutdown_quietly",
    ),
)


def _call_names(node: ast.AST) -> set[str]:
    """Every name referenced anywhere inside *node*, called or merely handed over.

    References, not just calls: the dashboard's force-kill passes the killer to
    ``run_in_executor`` and to a thread's ``target``, so a call-only scan sees no
    kill on the one path where the release matters most.
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            found.add(sub.attr)
        elif isinstance(sub, ast.Name):
            found.add(sub.id)
    return found


def _blocks(tree: ast.AST) -> list[list[ast.stmt]]:
    """Every statement list in *tree* -- a function body, an except handler, a
    with/if/try body. A release and the kill it guards must share one."""
    out: list[list[ast.stmt]] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                out.append(block)
    return out


@pytest.mark.parametrize(("rel_path", "release", "kill"), _RELEASE_BEFORE_KILL)
def test_a_post_registration_kill_releases_its_lease_first(
    rel_path: str, release: str, kill: str
) -> None:
    """Commenting out a release site must fail HERE, not in production.

    Without the release the gate sees a lease still outstanding and refuses the
    very kill this path exists to perform, so the process leaks -- and the only
    evidence is a REFUSED line in a log nobody reads. The ordering is the whole
    invariant: a release AFTER the kill is as broken as none at all.
    """
    tree = ast.parse((SRC.parents[1] / rel_path).read_text(encoding="utf-8"))
    for block in _blocks(tree):
        released_at: int | None = None
        for stmt in block:
            # A def or class is scanned as its own block. Counting one here would
            # let a release in one function pair with a kill in an unrelated later
            # one and pass on nothing.
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            names = _call_names(stmt)
            if kill in names and released_at is not None and released_at < stmt.lineno:
                return
            if release in names:
                released_at = stmt.lineno
    raise AssertionError(
        f"{rel_path}: no block calls {release}() before {kill}() -- a kill that "
        "does not release first is refused by the ownership gate and leaks the tree"
    )


def test_the_leaked_provider_killer_commits_the_teardown_before_it_signals() -> None:
    """The gate's verdict is a statement about the past, and this killer is where
    that matters: it runs on an executor thread while a session-sharing subagent
    claims its turn on the event loop, and between the verdict and the first signal
    sit a start-id read, a group resolution and an unbounded descendant walk.

    Re-reading before each signal is not enough, which is why this pins a COMMIT
    instead. A signal cannot be recalled: by the SIGKILL round the SIGTERM is long
    delivered and its grace -- ample time for a shared turn to start -- has passed,
    so a claim taken in that grace dies whatever the later rounds decide. The window
    has to be closed to new tenants, once, immediately before the first signal.

    And every commit needs its release: a barrier left standing refuses that pid's
    tenancies for the life of the gateway, so the paired call is pinned too.
    """
    tree = _parse(SRC / "session_pid.py")
    assert tree is not None
    target = _function_named(tree, "_sync_kill_provider")
    assert target is not None
    calls = _call_names(target)
    assert (
        "tenancy_epoch" in calls
    ), "_sync_kill_provider does not capture the tenancy epoch beside the gate's verdict"

    def _is_guard(node: ast.AST) -> bool:
        """An ``if not _commit_teardown(...)`` whose body leaves the function."""
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            return False
        if not (
            isinstance(test.operand, ast.Call) and _callee_name(test.operand) == "_commit_teardown"
        ):
            return False
        return any(isinstance(leaf, (ast.Return, ast.Raise)) for leaf in ast.walk(node))

    guards = [node for node in ast.walk(target) if _is_guard(node)]
    assert len(guards) >= 2, (
        f"_sync_kill_provider has {len(guards)} teardown-commit guard(s), expected at least "
        "2 -- one before the Windows tree kill and one before the POSIX escalation, since "
        "a verdict checked once cannot see a turn that starts during the grace and a "
        "delivered signal cannot be taken back"
    )
    releases = [
        node
        for node in ast.walk(target)
        if isinstance(node, ast.Try)
        and node.finalbody
        and any(
            _callee_name(leaf) == "release_runtime_teardown"
            for body in (node.finalbody,)
            for stmt in body
            for leaf in ast.walk(stmt)
            if isinstance(leaf, ast.Call)
        )
    ]
    assert len(releases) >= 2, (
        f"_sync_kill_provider drops the teardown barrier in {len(releases)} finally block(s), "
        "expected at least 2 -- one per signal path, because a barrier left standing refuses "
        "that pid's tenancies for the life of the gateway"
    )


# -- the shared-turn tenancy at the subagent's turn entry points --

#: Every method that drives a TURN on a provider that may be session-sharing. A
#: subagent holds no lease, so this claim is the only thing standing between a
#: live turn and the provider drain; one of these left unguarded is a turn that
#: can still be SIGTERMed mid-flight.
_TURN_ENTRY_POINTS = ("stream", "stream_command")


@pytest.mark.parametrize("function", _TURN_ENTRY_POINTS)
def test_a_shared_turn_claims_tenancy_and_releases_it_in_a_finally(function: str) -> None:
    """Commenting out either half must fail HERE, not in production.

    Without the claim, the drain that follows a principal's teardown finds nothing
    in the gate's registry and signals a live subagent turn. Without a ``finally``
    the claim leaks on exactly the paths that matter -- a reaped or failed turn is
    the case that leaves an orphan -- and a leaked claim refuses that pid's kills
    for the life of the gateway.
    """
    tree = _parse(SRC / "acp" / "session_provider.py")
    assert tree is not None
    target = _function_named(tree, function)
    assert target is not None, f"session_provider.{function} is gone; this pin needs rewriting"
    names = _call_names(target)
    assert "_claim_shared_turn" in names, (
        f"session_provider.{function} does not claim tenancy for the turn: a "
        "session-sharing subagent holds no lease, so nothing would refuse a drain "
        "that arrives mid-turn"
    )
    released_in_finally = any(
        "_end_shared_turn" in _call_names(stmt)
        for node in ast.walk(target)
        if isinstance(node, ast.Try)
        for stmt in node.finalbody
    )
    assert released_in_finally, (
        f"session_provider.{function} does not release its tenancy in a finally: a "
        "cancelled or failed turn would keep defending the process forever"
    )
