"""Pins for the memory-store seam gate, ``scripts/check_memory_store_seam.py``.

A named memory store is a separate SQLite silo. A memory-context call site that
does not name one reads whatever the resolver defaults to — the operator's own
``default`` store — for a crew that configured otherwise, and nothing goes red:
the prompt is well-formed and the content is plausible. The gate exists to make
that unrepresentable on added lines, so a gate that has silently stopped
matching is worse than no gate at all. These tests are what makes that visible.

Two of them are the anti-fooling probes, and they matter more than the rest:
:func:`test_similar_names_in_the_persistence_path_are_not_reported` pins that
matching is AST-based rather than textual (a substring implementation would hit
every ``_build_message_entry`` in the chat-persistence path), and
:func:`test_prose_naming_the_call_is_not_reported` pins the same property from
the other direction.
"""

from __future__ import annotations

import ast
import collections
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GATE_PATH = _REPO_ROOT / "scripts" / "check_memory_store_seam.py"

# The whole-tree backlog as it stands, per rule. Diff mode is what enforces
# today, so this is the count the gate REPORTS without failing; it shrinks as
# each call site is converted and the shrink is a deliberate edit here. A count
# that GREW means a new undeclared call site landed on a line the diff gate did
# not see.
#
# A rule with nothing left to report is ABSENT rather than zero: the comparison
# below is against a ``Counter``, which never mints a zero entry, so a rule listed
# at 0 could only ever fail. ``resolver-memory-root-required`` is in that state —
# both of its sites now name the global store, and
# :func:`test_no_resolver_site_reaches_its_root_by_omission` is what keeps them
# naming it rather than silently drifting back to omission.
# The remaining call has no crew identity: the process-wide heartbeat uses
# Global context on the fixed heartbeat template. Changed calls must still name
# their store explicitly; the offline evaluator names DEFAULT_MEMORY_STORE.
EXPECTED_BACKLOG = {
    # 16 before subagent runs were converted, then 15. The eight that came off
    # next are the surfaces that DO hold an identity: Slack (native and
    # transport), Discord, Telegram, the shared messaging pipeline, auto-nudge,
    # and both subagent-completion injections now resolve the session's own
    # recorded binding through `context.session_store_for_turn`, so a
    # conversation bound to a crew reads that crew's silo over every channel
    # instead of the operator's global store.
    # Named cron jobs now persist member_id and resolve their bound store at
    # execution, removing the two scheduled-context sites from this backlog.
    # Webhooks and task planner/executor continuations now preserve the trusted
    # originating session binding, removing three more sites.
    "store-identity-required": 1,
}

# A call in the shape every site actually uses: the bound method is handed to the
# embed pool as a VALUE, so the keywords land on the ENCLOSING call. There are no
# direct ``build_message(...)`` calls under ``src/``, which is the whole reason
# the gate has to walk the AST.
_POOL_CALL = """\
full_message, _ = await run_in_embed_pool(
    state.context_builder.build_message,
    message,
    is_new,
    session_key,
    agent=kiro_agent or slot.agent or None,{store}
)
"""


def _pool_call(store: str | None = None) -> str:
    return _POOL_CALL.format(store="" if store is None else f"\n    memory_store={store},")


# The resolver call sites that read the OPERATOR's own memory for a dashboard
# panel, held by path because a line number goes stale on any edit above the call.
_OPERATOR_MEMORY_PANELS = ("src/kiro_crew/suggestions.py", "src/kiro_crew/tips.py")

#: The constant those panels must name to stay on the global path deliberately.
#: Its existence is pinned separately, by
#: :func:`test_every_fix_message_names_symbols_that_exist`, since the resolver
#: rule's remedy prescribes it.
_DEFAULT_STORE_SYMBOL = "DEFAULT_MEMORY_STORE"


def _resolver_calls(gate: ModuleType, path: str) -> list[ast.Call]:
    """Every ``get_memory_for`` / ``get_lessons_for`` CALL in ``path``.

    Found through the gate's own ``RESOLVER_FUNCTIONS`` and name resolver, so a
    resolver renamed in one place cannot leave this probe scanning for a function
    that does not exist while still passing.
    """
    tree = ast.parse((_REPO_ROOT / path).read_text(encoding="utf-8"), filename=path)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and gate._call_func_name(node) in gate.RESOLVER_FUNCTIONS
    ]


@pytest.fixture(scope="module")
def gate() -> Iterator[ModuleType]:
    """The gate, loaded by path.

    ``scripts/`` is not a package, so an import would resolve only by accident of
    ``sys.path``. The ``sys.modules`` entry is not optional: ``@dataclass`` looks
    the defining module up there to resolve a string annotation (the gate uses
    ``from __future__ import annotations``), and a missing entry makes
    ``exec_module`` raise before the first rule is defined. It is removed again on
    teardown so nothing outlives this file on the worker.
    """
    name = "check_memory_store_seam"
    spec = importlib.util.spec_from_file_location(name, _GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def whole_tree(gate: ModuleType) -> list:
    """Every violation in the tracked tree, scanned once for the whole module."""
    return [v for path in gate.tracked_files() for v in gate.scan_file(path)]


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged = {k: v for k, v in os.environ.items() if k != "MEMSTORE_BASE_REF"}
    merged.update(env or {})
    return subprocess.run(
        [sys.executable, str(_GATE_PATH), *args],
        capture_output=True,
        text=True,
        # The gate's clean verdict is ASCII today, but its violation excerpts are
        # source lines, which can hold any byte; decoding with the locale code
        # page would mojibake them on Windows.
        encoding="utf-8",
        errors="replace",
        # The gate resolves everything from its own REPO_ROOT, but a child
        # inherits pytest's CWD (the checkout) and could write there; pin it to
        # the checkout explicitly rather than by accident.
        cwd=str(_REPO_ROOT),
        env=merged,
    )


# ---------------------------------------------------------------------------
# The gate still detects what it claims to
# ---------------------------------------------------------------------------


def test_gate_self_test_passes() -> None:
    """Every rule is exercised against a planted probe, in the gate's own mode.

    CI runs this same self-test before the real check. Running it from pytest too
    means a rule that stopped matching fails a local test run as well.
    """
    result = _run(["--test"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_fix_message_names_symbols_that_exist(gate: ModuleType) -> None:
    """A remedy is only a remedy if the call it prescribes is the call to make.

    A gate whose failure message names a parameter, function, or constant the tree
    does not have is worse than a silent one: a contributor who obeys it writes an
    import that cannot resolve, or — the sharper case — passes a value into a
    parameter resolved through a DIFFERENT namespace and gets the silent default
    read this gate exists to prevent. So every snake_case symbol any message names
    must be findable in the code it is talking about.
    """
    # The gate's OWN source is deliberately NOT in the haystack. Every symbol a
    # message names appears in the message text, so including the file that holds
    # the messages makes `missing` empty by construction and the probe can never
    # fail.
    haystack = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((_REPO_ROOT / "src" / "kiro_crew").rglob("*.py"))
    )
    named: dict[str, list[str]] = {}
    for rule in gate.ALL_RULES:
        # Matches SCREAMING_SNAKE as well as snake_case: a lowercase-only pattern
        # never extracts DEFAULT_MEMORY_STORE, which is the exact symbol the
        # bare-literal remedy prescribes and therefore the one most worth checking.
        for token in re.findall(
            r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", f"{rule.message} {rule.fix}"
        ):
            named.setdefault(token, []).append(rule.rule_id)
    assert named, "probe went vacuous: no symbol extracted from any message"
    missing = {token: ids for token, ids in named.items() if token not in haystack}
    assert not missing, f"messages name symbols that do not exist: {missing}"

    # The resolver rule is the one that can go wrong while every symbol in it
    # exists, because it governs TWO namespaces: a remedy naming only the silo arm
    # leaves a caller that genuinely wants the global store with no spelling but
    # omission, which is the shape the rule refuses.
    resolver_fix = gate.RULE_RESOLVER_ARGUMENT.fix
    assert f"{gate.STORE_KEYWORD}=" in resolver_fix
    assert _DEFAULT_STORE_SYMBOL in resolver_fix


def test_every_rule_has_a_probe(gate: ModuleType) -> None:
    """A rule with no probe can silently stop matching.

    The gate's own ``--test`` asserts this too; asserting it here as well means
    adding a rule without a probe cannot land by way of a green CI job whose
    self-test step was reordered away.
    """
    covered = {expected for _, _, _, expected in gate.PROBES if expected}
    covered |= {expected for _, _, expected in gate._EMPTY_REASON_PROBES}
    declared = {r.rule_id for r in gate.RULES} | {gate.RULE_UNPARSEABLE.rule_id}
    assert declared <= covered, f"no probe exercises {sorted(declared - covered)}"


# ---------------------------------------------------------------------------
# Today's tree
# ---------------------------------------------------------------------------


def test_whole_tree_mode_reports_without_enforcing(gate: ModuleType, whole_tree: list) -> None:
    """With no base ref the gate reports the backlog and exits 0.

    The operator heartbeat and offline evaluation coordinator intentionally
    retain Global context. Their exact backlog is pinned separately; changed
    unscoped calls remain subject to diff enforcement.
    """
    assert gate.report(whole_tree, enforcing=False, base=None) == 0


def test_whole_tree_backlog_is_exactly_todays_count(whole_tree: list) -> None:
    """The remaining backlog is a pinned fact, per rule.

    Update the numbers DOWN in the same change that converts a call site. A
    number that has to go up is the gate telling you an undeclared site landed.
    """
    counts = collections.Counter(v.rule.rule_id for v in whole_tree)
    assert dict(counts) == EXPECTED_BACKLOG


def test_no_resolver_site_reaches_its_root_by_omission(gate: ModuleType, whole_tree: list) -> None:
    """Every resolver call in the tree names the root it reads, by name.

    Two halves, because the first alone is satisfied by a rule that stopped
    matching. The sites the resolver rule was written for are the dashboard panels
    in ``suggestions.py`` and ``tips.py``, which summarize the OPERATOR's own
    memory: they are on the v1 path DELIBERATELY, and spelling that as
    ``memory_store=DEFAULT_MEMORY_STORE`` is what makes it reviewable — reached by
    omission instead, a deliberate global read is indistinguishable from a crew's
    silo leaking into a dashboard panel, and a rebinding to some crew's store would
    read as a no-op diff.

    Pinned by PATH and by AST, never by line number: a line pin goes stale on any
    edit above the call, and this file's own thesis is that matching is structural
    rather than textual.
    """
    reported = sorted(
        f"{v.path}:{v.line_no}"
        for v in whole_tree
        if v.rule.rule_id == "resolver-memory-root-required"
    )
    assert reported == []

    for path in _OPERATOR_MEMORY_PANELS:
        calls = _resolver_calls(gate, path)
        assert calls, f"probe went vacuous: {path} holds no resolver call at all"
        for call in calls:
            named = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
            assert _DEFAULT_STORE_SYMBOL in named, (
                f"{path} resolves memory without naming {_DEFAULT_STORE_SYMBOL}; this "
                f"surface summarizes the operator's own memory and must not inherit "
                f"whichever crew spoke last"
            )


# ---------------------------------------------------------------------------
# One seeded violation per rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        # 1. Unconditional: the sites that pass neither agent= nor a store are
        # exactly the ones a "has agent= but no store" rule would miss.
        pytest.param(_pool_call(), "store-identity-required", id="no-store-keyword"),
        pytest.param(
            "ctx = builder.build_session_context(session_key=session_key)\n",
            "store-identity-required",
            id="no-store-on-a-direct-call",
        ),
        # 2. `agent=` carries a kiro-cli modeId, a namespace disjoint from
        # cfg.agents, so a store derived from it fails silently toward default.
        pytest.param(
            _pool_call("kiro_agent"),
            "store-not-derived-from-agent",
            id="store-derived-from-an-agent",
        ),
        pytest.param(
            _pool_call("cfg.agents[agent_name].memory_store"),
            "store-not-derived-from-agent",
            id="store-resolved-from-an-agent-inline",
        ),
        # 3. A literal is not greppable as "default on purpose", and a typo in
        # one is a silent read of a store nobody configured.
        pytest.param(_pool_call('"default"'), "bare-store-literal", id="store-as-a-bare-literal"),
        # 4. `memory_store=None` satisfies the keyword rule and is not a string, so
        # every other rule lets it past — while the consumer's
        # `memory_store or workspace` resolves it to exactly what passing no
        # keyword at all resolves to. It is omission that looks reviewed.
        pytest.param(_pool_call("None"), "literal-none-store", id="store-as-a-literal-none"),
        # 5. Stashing the bound method is how a call would launder past a gate
        # that only reads call keywords.
        pytest.param(
            "builder = state.context_builder.build_message\n",
            "unanchored-target-reference",
            id="target-stashed-in-a-local",
        ),
        pytest.param(
            "await run_in_embed_pool(lambda: self.ctx_builder.build_session_context)\n",
            "unanchored-target-reference",
            id="target-deferred-into-a-lambda",
        ),
        # 6. BOTH resolver parameters are optional, so an empty call means
        # "whatever the fallback names" and says nothing about wanting it. The rule
        # asks for the ROOT rather than for a store specifically, because a
        # workspace root is a legitimate answer on the v1 path.
        pytest.param(
            "memory = ContextBuilder.get_memory_for()\n",
            "resolver-memory-root-required",
            id="resolver-with-no-argument",
        ),
        pytest.param(
            "memory = ContextBuilder.get_memory_for(None)\n",
            "resolver-memory-root-required",
            id="resolver-with-a-literal-none",
        ),
        pytest.param(
            "lessons = state.context_builder.get_lessons_for(workspace=None)\n",
            "resolver-memory-root-required",
            id="resolver-with-a-keyword-none",
        ),
    ],
)
def test_a_seeded_violation_is_reported_under_its_own_rule(
    gate: ModuleType, source: str, expected: str
) -> None:
    """Each rule fires, and fires under its own name.

    Membership rather than first-hit: one call can violate two rules at once,
    and asserting a single verdict would pin the walk order instead of the rule.
    """
    got = {v.rule.rule_id for v in gate.scan_source("src/kiro_crew/probe.py", source)}
    assert expected in got, f"expected {expected!r}, got {sorted(got) or 'no hit'}"


@pytest.mark.parametrize(
    "store",
    [
        "memory_store",
        "cfg.default_memory_store",
        "bindings.memory_store_name",
        # An expression whose value the reader can follow to a resolution. Only a
        # LITERAL None is omission-as-default; this one may still evaluate to None,
        # and what the gate can see is that a resolved name was consulted first.
        "store_name or None",
    ],
)
def test_a_declared_store_is_accepted(gate: ModuleType, store: str) -> None:
    """The forms a converted call site is meant to use produce no hit.

    The accepted spellings are symbols that exist: ``resolve_agent_bindings``
    returns ``memory_store_name`` and the config carries ``default_memory_store``.
    A fix message naming a constant the tree does not define sends a contributor
    to write an import that cannot resolve.
    """
    assert gate.scan_source("src/kiro_crew/probe.py", _pool_call(store)) == []


def test_the_definitions_themselves_are_not_call_sites(gate: ModuleType) -> None:
    """A ``def build_message`` is not a call, and neither is a resolver's default.

    The gate matches an ``ast.Attribute``, so ``context.py``'s own definitions —
    including the two ``str | None = None`` roots in a resolver's signature — are
    invisible to it. A rule reading argument defaults would flag the definition
    of the very thing it is protecting.
    """
    source = """\
class ContextBuilder:
    @staticmethod
    def get_memory_for(
        workspace: str | None = None, memory_store: str | None = None
    ) -> MemoryStore:
        key, store_name = _target_key(workspace, memory_store)
        return _memory_stores[key]

    def build_session_context(self, session_key, memory_store=None):
        return ""

    def build_message(self, message, is_new, session_key, memory_store=None):
        return self.build_session_context(session_key, memory_store=memory_store)
"""
    assert gate.scan_source("src/kiro_crew/context.py", source) == []


# ---------------------------------------------------------------------------
# An unparseable file is a failure, never a skip
# ---------------------------------------------------------------------------


def test_unparseable_file_fails_rather_than_being_skipped(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reporting nothing for a file nothing was checked in is a silent gate.

    Planted in a temp tree with ``REPO_ROOT`` repointed at it: writing a broken
    module into the real ``src/`` would leave it behind for every later test on
    this worker if this one failed mid-way.
    """
    monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))

    planted = "probe_memory.py"
    (tmp_path / planted).write_text(
        "full_message = await run_in_embed_pool(\n    ctx.build_message,\n",
        encoding="utf-8",
    )
    assert gate.main([planted]) == 1
    assert [v.rule.rule_id for v in gate.scan_file(planted)] == ["unparseable-source"]

    (tmp_path / planted).write_text(_pool_call("memory_store"), encoding="utf-8")
    assert gate.main([planted]) == 0


def test_a_file_that_is_not_utf8_text_fails(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable is the same defect as unparseable: nothing in it was checked."""
    monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
    planted = "probe_bytes.py"
    (tmp_path / planted).write_bytes(b"ctx.build_message\n\xff\xfe not utf-8\n")
    assert gate.main([planted]) == 1


def test_a_file_with_no_call_site_is_skipped_without_parsing(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substring prefilter is sound, and is not the matching rule.

    An identifier the AST would report is always present in the text, so a file
    naming none of them holds no call site whether or not it parses. That is what
    lets the gate skip 1,400 files without ever matching on a substring.
    """
    monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
    planted = "probe_unrelated.py"
    (tmp_path / planted).write_text("def f(:\n", encoding="utf-8")
    assert gate.scan_file(planted) == []


# ---------------------------------------------------------------------------
# Diff scoping
#
# There is deliberately no test that runs the gate in diff mode against a live
# ``origin/main``. The shared plumbing measures base-to-WORKING-TREE
# (``ratchet_scope.added_lines_at``), which is what makes a local run useful and
# also what makes such a test's verdict a property of whatever the contributor
# has uncommitted rather than of the gate: an unrelated edit under
# ``src/kiro_crew/`` turns it red, and a checkout with no ``origin/main`` turns it
# into a skip that reads as a pass. CI's ``memory-store-seam`` job is what
# enforces against the PR's real base; what needs pinning here is the scoping
# RULE, done below with the plumbing pinned so the answer is deterministic. The
# sibling gate's ``test/test_harness_parity.py`` carries no such test either.
# ---------------------------------------------------------------------------


def _pin_diff(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, path: str, touched: set[int]
) -> None:
    """Point the diff plumbing at one path with one known touched-line set."""
    monkeypatch.setattr(gate, "diff_base", lambda base: "BASE")
    monkeypatch.setattr(gate, "changed_paths", lambda frm: [path])
    monkeypatch.setattr(gate, "touched_lines", lambda frm, p: touched)


def test_an_added_line_inside_a_call_brings_the_whole_call_into_scope(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope is the node's span, not the anchor line alone.

    A memory-context call spans a dozen lines, and editing any one of them is a
    change to what that call reads. Scoping to the opening line only would let a
    keyword be added three lines down without the store ever being reviewed.
    """
    monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
    planted = "src/kiro_crew/probe_memory.py"
    (tmp_path / "src" / "kiro_crew").mkdir(parents=True)
    (tmp_path / planted).write_text(_pool_call(), encoding="utf-8")

    _pin_diff(gate, monkeypatch, planted, {4})
    assert gate.enforce_diff("origin/main") == 1

    # A line outside the call's span leaves it alone.
    _pin_diff(gate, monkeypatch, planted, {99})
    assert gate.enforce_diff("origin/main") == 0


def test_a_changed_unparseable_file_fails_diff_mode_whatever_line_moved(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable file has no span to intersect, so scope must not excuse it.

    This is the shape in which a diff-scoped gate stops gating: the file the
    change touched reports nothing, the run is green, and nothing says why.
    """
    monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
    planted = "src/kiro_crew/probe_bytes.py"
    (tmp_path / "src" / "kiro_crew").mkdir(parents=True)
    (tmp_path / planted).write_bytes(b"ctx.build_message\n\xff\xfe\n")

    _pin_diff(gate, monkeypatch, planted, {500})
    assert gate.enforce_diff("origin/main") == 1


def test_removing_a_store_keyword_is_a_touched_line(gate: ModuleType, tmp_path: Path) -> None:
    """A deletion-only hunk is anchored, so dropping the store is not invisible.

    Deleting a ``memory_store=`` line adds NOTHING: git emits ``+<start>,0`` and
    every other line of the call stays byte-identical, so a pure added-line scope
    contributes no line for the call and the gate that exists to keep the store
    declared would let it be removed. Driven through a real repository, because
    the property being pinned is what git actually emits for that edit.
    """
    repo = tmp_path / "r"
    repo.mkdir()

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        ).stdout

    run("init", "-q")
    run("config", "user.email", "probe@example.invalid")
    run("config", "user.name", "probe")
    declared = _pool_call("memory_store")
    (repo / "a.py").write_text(declared, encoding="utf-8")
    run("add", "a.py")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").strip()

    stripped = "".join(ln for ln in declared.splitlines(keepends=True) if "memory_store=" not in ln)
    (repo / "a.py").write_text(stripped, encoding="utf-8")
    run("add", "a.py")
    run("commit", "-qm", "drop the store")
    diff = run("diff", "--unified=0", "--no-color", base, "HEAD", "--", "a.py")

    assert any(re.search(r"\+\d+,0", ln) for ln in diff.splitlines() if ln.startswith("@@")), diff
    anchored = gate.hunk_touched_lines(diff)
    assert anchored, "a deletion-only hunk reported nothing touched"

    # The anchored line must land inside the span of the violation the deletion
    # created, or the scope intersection still drops it.
    found = gate.scan_source("src/kiro_crew/probe.py", stripped)
    assert [v.rule.rule_id for v in found] == ["store-identity-required"]
    assert any(v.span[0] <= n <= v.span[1] for v in found for n in anchored)


def test_the_production_touched_line_reader_anchors_deletions_too(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``touched_lines`` and the self-test's probe share one anchoring decision.

    Two independent keyword arguments would let the production reader be written
    without anchoring while the probe stayed green — the gate would then report a
    removed store only in its own self-test.
    """
    seen: dict[str, object] = {}

    def spy(frm: str, path: str, *, anchor_deletions: bool = False) -> set[int]:
        seen["anchor_deletions"] = anchor_deletions
        return {1}

    monkeypatch.setattr(gate._scope(), "added_lines_at", spy)
    assert gate.touched_lines("BASE", "src/kiro_crew/x.py") == {1}
    assert seen == {"anchor_deletions": True}


# ---------------------------------------------------------------------------
# The escape hatch
# ---------------------------------------------------------------------------


def test_the_escape_hatch_silences_the_call(gate: ModuleType) -> None:
    """A marker WITH a reason, on the call's opening line, suppresses it."""
    source = (
        "full_message, _ = await run_in_embed_pool(  # memory-store-ok: heartbeat "
        "has no crew binding\n"
        "    self.ctx_builder.build_message,\n"
        "    injected,\n"
        "    is_new,\n"
        ")\n"
    )
    assert gate.scan_source("src/kiro_crew/slack/gateway.py", source) == []


@pytest.mark.parametrize(
    "marker",
    ["# memory-store-ok:", "# memory-store-ok:   ", "# memory-store-ok", "# memory-store-ok :x"],
    ids=["empty", "whitespace-only", "no-colon", "space-before-colon"],
)
def test_an_escape_hatch_without_a_reason_does_not_silence(gate: ModuleType, marker: str) -> None:
    """An unexplained waiver is how a silo leak gets reviewed as formatting.

    The reason is what a reviewer reads to decide whether the call really has no
    store to name, so a marker without one must not suppress anything.
    """
    source = (
        f"full_message, _ = await run_in_embed_pool(  {marker}\n"
        "    self.ctx_builder.build_message,\n"
        "    injected,\n"
        ")\n"
    )
    got = {v.rule.rule_id for v in gate.scan_source("src/kiro_crew/slack/gateway.py", source)}
    assert "store-identity-required" in got


def test_the_marker_must_be_on_the_calls_opening_line(gate: ModuleType) -> None:
    """The anchor is the opening line, not any line of the call.

    That is where a reader looks for the store and where the keywords begin, so
    a marker buried in the argument list does not silence the call.
    """
    source = (
        "full_message, _ = await run_in_embed_pool(\n"
        "    self.ctx_builder.build_message,  # memory-store-ok: buried\n"
        "    injected,\n"
        ")\n"
    )
    got = {v.rule.rule_id for v in gate.scan_source("src/kiro_crew/slack/gateway.py", source)}
    assert "store-identity-required" in got


# ---------------------------------------------------------------------------
# The two probes that stop this gate from being fooled
# ---------------------------------------------------------------------------


def test_similar_names_in_the_persistence_path_are_not_reported(whole_tree: list) -> None:
    """Matching is AST-based, not textual.

    ``_build_message_entry`` and ``_build_message_entry_uncached`` CONTAIN
    ``build_message`` and have nothing to do with memory: they build a history
    entry from a chat message. A substring implementation would report every one
    of them, and the noise is what would get the gate turned off.

    The count of such lines is measured rather than restated so the probe cannot
    go vacuous if the persistence path is renamed to death.
    """
    similar: list[str] = []
    for path in sorted((_REPO_ROOT / "src" / "kiro_crew").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "_build_message" in line:
                similar.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{i}")
    assert len(similar) >= 10, f"probe went vacuous: only {similar}"

    reported = {f"{v.path}:{v.line_no}" for v in whole_tree}
    assert not reported & set(similar), f"substring match reported {reported & set(similar)}"


def test_a_similar_name_spelled_as_an_attribute_is_not_reported(gate: ModuleType) -> None:
    """The name comparison is equality, not containment.

    The tree's own ``_build_message_entry`` references are plain names, so they
    would survive a scanner that matched attribute names by containment — this
    spelling is what closes that half. It is the same helper, reached through its
    module, and it still has nothing to do with memory.
    """
    source = """\
entry = chat_persistence._build_message_entry(m)
builder = chat_persistence._build_message_entry_uncached
"""
    assert gate.scan_source("src/kiro_crew/history.py", source) == []


def test_prose_naming_the_call_is_not_reported(gate: ModuleType) -> None:
    """Naming the forbidden form in order to explain it is not the form.

    The same property as the probe above, from the other direction: this tree
    carries dozens of comments and docstrings that name ``build_message`` while
    explaining what it does to the prompt.
    """
    source = '''\
"""Authoritative bounds from build_message(...) — no reconstruction needed.

``ContextBuilder.build_message`` gates memory, lessons, skills and prior
sessions; ``build_session_context(...)`` prepends the memory block.
"""

# Off-loop: build_message(...) embeds the episodic query (blocking urllib), and
# ContextBuilder.get_memory_for(None) is the spelling that reads the global store.
NOTE = "build_message and build_session_context and get_memory_for()"
'''
    assert gate.scan_source("src/kiro_crew/context_blocks.py", source) == []
