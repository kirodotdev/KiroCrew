#!/usr/bin/env python3
"""check_memory_store_seam.py — every memory-context call site names its store.

A named memory store (``cfg.agents[<crew>].memory_store``) is a SILO: a separate
SQLite file, seeded empty, never inferred. The two entry points that read memory
into a prompt — ``ContextBuilder.build_message`` and
``ContextBuilder.build_session_context`` — therefore have to be told which silo
they are reading, at every call site, or the omission resolves to the operator's
own ``default`` store and the crew that configured otherwise reads someone
else's memory. Nothing goes red when that happens: the prompt is well-formed,
the store exists, and the content is plausible.

This gate catches omitted store arguments in explicit call sites on touched
lines. It is a source-review aid, not an authorization boundary: arbitrary
reflection and computed attribute names cannot be resolved by this AST check.
Literal reflective access to a target method is refused so the call must expose
its receiver and store argument to ordinary review. Runtime protected bindings,
store ownership checks and filesystem isolation enforce member boundaries.

## Why this cannot be a line regex

Every call site passes ``build_message`` as a VALUE, not as a call — the method
performs a blocking embed, so callers hand the bound method to
``run_in_embed_pool`` and the keywords land on THAT call::

    full_message, _ = await run_in_embed_pool(
        state.context_builder.build_message,
        message,
        is_new,
        session_key,
        agent=kiro_agent or slot.agent or None,
        memory_store=memory_store,
    )

There are ZERO direct ``build_message(...)`` calls under ``src/kiro_crew/``, so a
line-oriented rule would have to guess which of the following lines carries the
store — and a rule matching the same names as a SUBSTRING would additionally hit
every ``_build_message_entry`` in the chat-persistence path, which builds a
history entry from a chat message and has nothing to do with memory. So the
scanner matches the ``ast.Attribute`` by name EQUALITY, walks to the ENCLOSING
``ast.Call``, and reads that call's keywords.

## What the resolvers take: two namespaces, two arguments

``ContextBuilder.get_memory_for`` / ``get_lessons_for`` (``context.py:1932``,
``:1988``) take BOTH roots — ``workspace`` on the v1 path and ``memory_store``
for a crew's silo — and ``_target_key`` (``context.py:125``) resolves the pair,
a named store winning outright. Collapsing them into one argument is the defect
the split exists to prevent: a crew bound to store ``acme`` and a workspace also
called ``acme`` shared one cache slot, so whichever was built first decided where
the other one read.

Both parameters are optional, which is why the rule about these two functions
exists at all: a call with nothing in it reads the global store and says nothing
about having wanted it. So the remedy names the arm the caller means —
``memory_store=<resolved name>`` for a silo, or
``memory_store=DEFAULT_MEMORY_STORE`` to stay on the global path DELIBERATELY,
which is what a surface summarizing the operator's OWN memory wants
(``suggestions.py`` and ``tips.py``, the dashboard panels). Reaching the global
store by omission instead is what makes a deliberate global read
indistinguishable from a crew's silo leaking into a dashboard panel.

## Why the WRITE side has no rule here

Isolation is actually CREATED on the write path: the consolidator resolves
markdown, lessons and vectors from the session's own metadata and hands all three
to ``_write_structured_memory`` / ``_save_lessons``, whose store parameters
default to the global handles off ``self``. Same omission-means-default shape,
and still not gateable — the rule cannot be made precise:

* Nothing to read. Both are handed to ``run_in_embed_pool`` POSITIONALLY
  (``run_in_embed_pool(self._write_structured_memory, result, key, vector_store)``),
  so there is no keyword. A keyword rule reddens the one correct call site in the
  tree; an arity rule hardcodes each method's parameter count into this file and
  breaks when either grows a parameter.
* Decisively, no rule can tell the fix from the defect: ``vector_store`` and
  ``self._vector_store`` are one expression in the same argument slot, and the
  SECOND is what the workspace and default arms correctly pass. A rule that
  accepts the defect while reading as coverage is worse than no rule.
* And there is little left to cover: both are private, single-caller methods, and
  that caller resolves all three handles in the same function.

``ContextBuilder.ensure_store``, the third write-side handle, needs no rule
either — its parameter is REQUIRED, so omitting the store is a ``TypeError``
rather than a silent global write.

## Why a deletion counts as a touched line

A deletion-only hunk reads ``+<start>,0``: lines were removed and none added, so
a pure added-line scope contributes nothing for it. REMOVING an existing
``memory_store=`` keyword is exactly that shape — the rest of the call stays
byte-identical, and the store silently reverts to whatever the resolver defaults
to. Deletions are therefore ANCHORED at ``start`` (``_ANCHOR_DELETIONS``), which
falls inside the span of the call the keyword was removed from.

## Why diff-scoped

Diff mode is the standing enforcement policy. The operator heartbeat and offline
evaluation coordinator intentionally use Global context without a member store.
Whole-tree mode reports those calls without failing, while
``test/test_memory_store_seam.py`` pins the exact backlog against growth. New or
changed unscoped calls still fail in diff mode. This contract does not promise a
future whole-tree enforcement switch.

## Usage

    # enforce on the lines this branch touches (exit 1 on any violation)
    MEMSTORE_BASE_REF=origin/main python3 scripts/check_memory_store_seam.py

    # report the whole-tree backlog, enforce nothing (exit 0)
    python3 scripts/check_memory_store_seam.py

    # scan explicit files, ignoring git entirely (enforcing)
    python3 scripts/check_memory_store_seam.py src/kiro_crew/task_planner.py

    # self-test: plant one probe per rule, assert each verdict
    python3 scripts/check_memory_store_seam.py --test

## Escape hatch

A call the rules model wrongly can opt out with ``# memory-store-ok: <reason>``
on the call's OPENING line. The reason is mandatory and must be non-empty: an
unexplained marker is how a silo leak gets reviewed as a formatting change.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Iterable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Only the backend package holds memory-context call sites. The gate's own
# sources spell every forbidden shape out literally and live outside this root,
# so they need no exemption.
SCAN_ROOTS = ("src/kiro_crew/",)
SCAN_SUFFIX = ".py"

# The two methods that read a memory store into a prompt. Matched as attribute
# names, so a ``def build_message`` (the definition) and a same-named local do
# not count, and ``_build_message_entry`` — a different name — never matches.
TARGET_METHODS = frozenset({"build_message", "build_session_context"})

# The store keyword every enclosing call must carry.
STORE_KEYWORD = "memory_store"

# Names a store expression may not be derived from. ``agent=`` on these call
# sites carries a kiro-cli ``modeId`` (acp/client.py:3971 sends it as
# ``session/set_mode``'s ``modeId``), which is a namespace DISJOINT from
# ``cfg.agents`` — a kiro agent registered under ``~/.kiro/agents/`` is never
# added to ``cfg.agents`` at all. Deriving a store from one resolves to
# ``default`` with ``requested_resolved=False`` and reads the operator's real
# store for exactly the crew that configured otherwise. Resolution belongs to
# ``config.loader.resolve_agent_bindings`` (loader.py:4563), whose result the
# call site then passes by name.
AGENT_DERIVED_NAMES = frozenset({"agent", "kiro_agent", "agent_id", "_agent", "agent_name"})

# The lazy per-root resolvers (context.py:1932, :1988). BOTH parameters are
# optional — ``workspace`` on the v1 path, ``memory_store`` for a crew's silo — so
# a call with nothing in it silently means "whatever the fallback names" and says
# nothing about having wanted it.
RESOLVER_FUNCTIONS = frozenset({"get_memory_for", "get_lessons_for"})

# Read from the RAW opening line of the call: the marker lives in a comment, and
# the reason group must match at least one non-space character.
SUPPRESSION = re.compile(r"#\s*memory-store-ok:(?P<reason>.*)$")

# Crossing one of these means the attribute is not an argument of the call above
# it — it is deferred into a callable, which is the laundering
# ``unanchored-target-reference`` exists to catch.
_CALL_BOUNDARY = (
    ast.Lambda,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


@dataclass(frozen=True)
class Rule:
    """One forbidden shape.

    ``fix`` is the form to write instead. It is printed with every violation,
    because a gate that only says no teaches nothing.
    """

    rule_id: str
    message: str
    fix: str


RULE_STORE_REQUIRED = Rule(
    rule_id="store-identity-required",
    message=(
        f"memory-context call with no {STORE_KEYWORD}= keyword, so it reads "
        f"whichever store the resolver defaults to"
    ),
    fix=(
        f"pass {STORE_KEYWORD}= on the enclosing call. This is UNCONDITIONAL: a rule "
        f"spelled 'has agent= but no store' would miss the sites that pass neither, "
        f"and identity by the ABSENCE of another argument is the shape AGENTS.md's "
        f"harness-parity section forbids"
    ),
)

RULE_NOT_AGENT_DERIVED = Rule(
    rule_id="store-not-derived-from-agent",
    message=(
        f"{STORE_KEYWORD}= derived from an agent name. `agent=` here carries a "
        f"kiro-cli modeId (acp/client.py:3971), a namespace DISJOINT from "
        f"cfg.agents, so resolve_agent_bindings(cfg, 'kirocrew-ops') answers store "
        f"`default` with requested_resolved=False — the derivation fails SILENTLY "
        f"toward the operator's real store, for exactly the crew that configured "
        f"otherwise"
    ),
    fix=(
        f"resolve the store once through config.loader.resolve_agent_bindings and "
        f"pass its name ({STORE_KEYWORD}=bindings.memory_store_name), or pass a "
        f"store name the caller already holds"
    ),
)

RULE_NO_BARE_LITERAL = Rule(
    rule_id="bare-store-literal",
    message=f"{STORE_KEYWORD}= given a bare string literal",
    fix=(
        f"pass the resolved name ({STORE_KEYWORD}=bindings.memory_store_name, from "
        f"config.loader.resolve_agent_bindings), or the configured default "
        f"({STORE_KEYWORD}=cfg.default_memory_store) when the default store IS the "
        f"answer — a literal is not greppable as that intent, and a misspelled one is "
        f"a silent read of a store nobody configured"
    ),
)

RULE_NONE_STORE = Rule(
    rule_id="literal-none-store",
    message=(
        f"{STORE_KEYWORD}=None, which is omission spelled out: the consumer hands the "
        f"pair to _target_key (context.py:125), which collapses a None store onto the "
        f"v1 path — so build_session_context's own resolver call (context.py:2716) "
        f"reads exactly what passing no keyword at all reads"
    ),
    fix=(
        f"pass the store the caller resolved ({STORE_KEYWORD}=bindings.memory_store_name), "
        f"or take the `# memory-store-ok: <reason>` hatch when the call genuinely has no "
        f"store to name. An explicit None is the one form that satisfies the keyword rule "
        f"while changing nothing, so it makes an unreviewed omission look reviewed"
    ),
)

RULE_UNANCHORED = Rule(
    rule_id="unanchored-target-reference",
    message=(
        "memory-context method reference is stashed, deferred or accessed through "
        "reflection, so its store argument cannot be checked"
    ),
    fix=(
        "call it, or hand it straight to the runner that calls it "
        "(run_in_embed_pool(...)) — stashing the bound method in a local or a "
        "lambda is how a call launders past this gate"
    ),
)

RULE_RESOLVER_ARGUMENT = Rule(
    rule_id="resolver-memory-root-required",
    message=(
        "per-root resolver called with no argument or a literal None, which is "
        "omission-as-default: both roots are optional, so the call reads the global "
        "store and nothing in it says that was the intent"
    ),
    fix=(
        f"name the root this caller reads. {STORE_KEYWORD}=<the resolved store name> "
        f"(config.loader.resolve_agent_bindings answers it as memory_store_name) for a "
        f"crew's own silo; {STORE_KEYWORD}=DEFAULT_MEMORY_STORE to stay on the global "
        f"path DELIBERATELY, which is what a surface summarizing the operator's OWN "
        f"memory wants. A workspace root is still the lone positional argument "
        f"(get_memory_for(ws_name)). The hatch is for a caller with no root to name at "
        f"all — reaching the global store by omission instead is what makes a deliberate "
        f"global read indistinguishable from a crew's silo leaking into it"
    ),
)

RULES: tuple[Rule, ...] = (
    RULE_STORE_REQUIRED,
    RULE_NOT_AGENT_DERIVED,
    RULE_NO_BARE_LITERAL,
    RULE_NONE_STORE,
    RULE_UNANCHORED,
    RULE_RESOLVER_ARGUMENT,
)

# Not a rule about a line shape: a file the scanner cannot parse has been
# CHECKED FOR NOTHING, and reporting nothing for it is how a gate quietly stops
# gating. It fails instead of skipping.
RULE_UNPARSEABLE = Rule(
    rule_id="unparseable-source",
    message="cannot be parsed as Python, so its memory-context call sites were never checked",
    fix="fix the syntax error — an unparseable file is a gate failure, never a skip",
)

# Every rule, including the one that is not a line shape. Self-test rule
# coverage is checked against THIS set.
ALL_RULES: tuple[Rule, ...] = (*RULES, RULE_UNPARSEABLE)


@dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    rule: Rule
    text: str
    # Inclusive [first, last] source span of the node this violation is about.
    # Diff scoping intersects THIS with the touched lines, so editing any line of
    # a multi-line call brings its store declaration back into review.
    span: tuple[int, int]

    def render(self) -> str:
        excerpt = self.text.strip()[:160]
        out = f"{self.path}:{self.line_no}: [{self.rule.rule_id}] {self.rule.message}"
        if excerpt:
            out += f"\n    {excerpt}"
        return out + f"\n    fix: {self.rule.fix}"


def in_scope(path: str) -> bool:
    if not path.endswith(SCAN_SUFFIX):
        return False
    return any(path.startswith(root) for root in SCAN_ROOTS)


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    table: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            table[child] = node
    return table


def enclosing_call(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.Call | None:
    """The nearest ancestor ``Call`` ``node`` is part of, or None.

    Stops at a function, lambda, or comprehension boundary: an attribute inside
    one of those is evaluated later, so the call surrounding the boundary never
    sees it as an argument and its keywords say nothing about it.
    """
    cur: ast.AST = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.Call):
            return cur
        if isinstance(cur, _CALL_BOUNDARY):
            return None
    return None


def _span(node: ast.AST) -> tuple[int, int]:
    first = getattr(node, "lineno", 1)
    last = getattr(node, "end_lineno", None) or first
    return (first, last)


def _named_in(expr: ast.AST) -> set[str]:
    """Every identifier the expression names, by ``Name.id`` or ``Attribute.attr``.

    A string constant is deliberately not a name: ``memory_store="agent"`` is a
    bare literal, judged by its own rule.
    """
    names: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def suppressed(lines: list[str], line_no: int) -> bool:
    """True when the raw source line carries a marker WITH a reason."""
    if not 1 <= line_no <= len(lines):
        return False
    match = SUPPRESSION.search(lines[line_no - 1])
    if match is None:
        return False
    return bool(match.group("reason").strip())


def _call_func_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _excerpt(lines: list[str], line_no: int) -> str:
    return lines[line_no - 1] if 1 <= line_no <= len(lines) else ""


def scan_source(path: str, source: str) -> list[Violation]:
    """Every violation in ``source``, whole-file."""
    # Substring PREFILTER, never substring matching. An identifier the AST would
    # report is always present in the text, so a file naming none of them holds
    # no call site whether or not it parses — which is what makes skipping it
    # sound while still failing on an unparseable file that could hold one. This
    # is the difference between a 17-file parse and a 1,400-file one; matching on
    # the same substrings would hit every `_build_message_entry` in the
    # chat-persistence path.
    if not any(name in source for name in (*TARGET_METHODS, *RESOLVER_FUNCTIONS)):
        return []

    lines = source.split("\n")
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        # ValueError covers a NUL byte, which ast.parse rejects without a
        # SyntaxError. Span is the whole file so any added line intersects it.
        return [
            Violation(path, 1, RULE_UNPARSEABLE, "", (1, max(1, len(lines)))),
        ]

    parents = _parents(tree)
    found: list[Violation] = []

    def add(rule: Rule, node: ast.AST, anchor: int) -> None:
        if suppressed(lines, anchor):
            return
        found.append(Violation(path, anchor, rule, _excerpt(lines, anchor), _span(node)))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _call_func_name(node) == "__getattribute__"
            and any(
                isinstance(arg, ast.Constant) and arg.value in TARGET_METHODS
                for arg in node.args
            )
        ):
            add(RULE_UNANCHORED, node, node.lineno)
            continue
        if (
            isinstance(node, ast.Call)
            and _call_func_name(node) in {"getattr", "methodcaller"}
        ):
            index = 1 if _call_func_name(node) == "getattr" else 0
            if (
                len(node.args) > index
                and isinstance(node.args[index], ast.Constant)
                and node.args[index].value in TARGET_METHODS
            ):
                add(RULE_UNANCHORED, node, node.lineno)
                continue
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in TARGET_METHODS
        ):
            add(RULE_UNANCHORED, node, node.lineno)
            continue
        if isinstance(node, ast.Attribute) and node.attr in TARGET_METHODS:
            call = enclosing_call(node, parents)
            if call is None:
                add(RULE_UNANCHORED, node, node.lineno)
                continue
            # The escape hatch and the report both anchor on the call's OPENING
            # line: that is where a reader looks for the store, and where the
            # keywords begin.
            anchor = call.lineno
            keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            store = keywords.get(STORE_KEYWORD)
            if store is None:
                # A ``**kwargs`` splat does not satisfy this: it hides the
                # store from every reader of the call site, which is what the
                # rule is for. A genuine forwarder takes the escape hatch.
                add(RULE_STORE_REQUIRED, call, anchor)
                continue
            if _named_in(store) & AGENT_DERIVED_NAMES:
                add(RULE_NOT_AGENT_DERIVED, call, anchor)
            if isinstance(store, ast.Constant) and store.value is None:
                # A LITERAL None only. ``store or None`` and ``resolved.name`` are
                # expressions whose value the reader can follow to a resolution;
                # ``memory_store=None`` is the keyword rule satisfied by a value
                # that resolves to the same store as passing nothing.
                add(RULE_NONE_STORE, call, anchor)
            if isinstance(store, ast.Constant) and isinstance(store.value, str):
                add(RULE_NO_BARE_LITERAL, call, anchor)

        elif isinstance(node, ast.Call) and _call_func_name(node) in RESOLVER_FUNCTIONS:
            # Positional and keyword values together, and ANY literal None among
            # them is a hit. With two optional roots that is stricter than "no root
            # was named" — `get_memory_for(None, store)` names a real store while
            # spelling the workspace as None — and it is kept that way on purpose:
            # the reader of that call cannot tell which namespace won without going
            # to ``_target_key`` to learn that the store wins outright. Making the
            # call spell itself out is the whole point, and a caller that genuinely
            # means it takes the escape hatch.
            #
            # A `*`/`**` splat is NOT an argument here, matching the store-keyword
            # rule above. Counting it as one lets `get_memory_for(**opts)` pass
            # while `**{}` is literally zero arguments and reads the default root
            # exactly as the bare call does — so the splat that hides the root from
            # the reader would be the one spelling the gate accepts. A genuine
            # forwarder takes the escape hatch.
            args: list[ast.expr] = [
                *(a for a in node.args if not isinstance(a, ast.Starred)),
                *(kw.value for kw in node.keywords if kw.arg),
            ]
            explicit_none = any(isinstance(a, ast.Constant) and a.value is None for a in args)
            if not args or explicit_none:
                add(RULE_RESOLVER_ARGUMENT, node, node.lineno)

    return found


def read_source(path: str) -> str | None:
    """File contents, or None when it cannot be read as UTF-8 text."""
    try:
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def scan_file(path: str) -> list[Violation]:
    """Whole-file violations, or an ``unparseable-source`` verdict.

    An unreadable file is reported, not skipped, for the same reason a
    syntactically broken one is: nothing in it was checked.
    """
    source = read_source(path)
    if source is None:
        return [Violation(path, 1, RULE_UNPARSEABLE, "", (1, 1))]
    return scan_source(path, source)


# ---------------------------------------------------------------------------
# git plumbing (same contract as scripts/check_harness_parity.py)
# ---------------------------------------------------------------------------


def git(args: list[str]) -> str:
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


_SCOPE_MODULE = None


def _scope():
    """The shared diff plumbing (see ``scripts/ratchet_scope.py``).

    Loaded by path, not imported: ``scripts/`` is not a package, so a plain
    import would resolve only by accident of ``sys.path[0]`` — and not at all
    when a test loads this gate by path. Shared so every diff-scoped gate agrees
    on which lines a change touched. Lazy, so the explicit-file and ``--test``
    modes never touch git.
    """
    global _SCOPE_MODULE
    if _SCOPE_MODULE is None:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratchet_scope.py")
        spec = importlib.util.spec_from_file_location("ratchet_scope", script)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SCOPE_MODULE = module
    return _SCOPE_MODULE


def diff_base(base: str) -> str:
    """The commit to measure against (see ``ratchet_scope.resolve_base``)."""
    return _scope().resolve_base(base)


def changed_paths(frm: str) -> list[str]:
    """In-scope paths this change touches (``ratchet_scope.changed_paths_at``)."""
    try:
        paths = _scope().changed_paths_at(frm)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::memory-store gate: cannot diff against {frm} — the base "
            f"commit is not present. Fetch it before running, or unset "
            f"MEMSTORE_BASE_REF to report whole-tree counts without "
            f"enforcing.\n{exc.stderr}"
        )
    return [p for p in paths if in_scope(p)]


# One decision, made once: a deletion-only hunk is ANCHORED, not dropped. Both
# consumers below read this constant, so the self-test's deletion probe (which
# goes through ``hunk_touched_lines``) covers the production path
# (``touched_lines``) — two independent keyword arguments would let the production
# one be dropped with the probe still green, restoring the exact hole the
# anchoring closes.
_ANCHOR_DELETIONS = True


def hunk_touched_lines(diff: str) -> set[int]:
    """1-based lines the hunk headers in one path's ``--unified=0`` diff mark.

    A named seam so the self-test's deletion probe drives the REAL parsing chain
    without a repository. Deletions are anchored because removing an existing
    ``memory_store=`` keyword adds NO line: the hunk reads ``+<start>,0``, the
    rest of the call is byte-identical, and a pure added-line scope would let the
    store be dropped from a converted call site unseen. ``start`` is where the
    removed lines sat, which is inside that call's span.
    """
    return _scope().parse_added_lines(diff, anchor_deletions=_ANCHOR_DELETIONS)


def touched_lines(frm: str, path: str) -> set[int]:
    """1-based line numbers this change adds to — or removes from — ``path``."""
    return _scope().added_lines_at(frm, path, anchor_deletions=_ANCHOR_DELETIONS)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

_LEGIT_CALL = """\
full_message, _ = await run_in_embed_pool(
    state.context_builder.build_message,
    message,
    is_new,
    session_key,
    agent=kiro_agent or slot.agent or None,
    memory_store={store},
)
"""

# (label, path, source, expected rule id or None)
PROBES: tuple[tuple[str, str, str, str | None], ...] = (
    (
        "store-missing-on-the-pool-call",
        "src/kiro_crew/dashboard/chat_runner.py",
        """\
full_message, _ = await run_in_embed_pool(
    state.context_builder.build_message,
    message,
    is_new,
    session_key,
    agent=kiro_agent or slot.agent or None,
)
""",
        "store-identity-required",
    ),
    (
        "store-missing-with-no-keywords-at-all",
        "src/kiro_crew/slack/gateway.py",
        "await run_in_embed_pool(self.ctx_builder.build_message, injected, is_new)\n",
        "store-identity-required",
    ),
    (
        "store-missing-on-a-direct-session-context-call",
        "src/kiro_crew/eval/runner.py",
        "memory_context = ctx_builder.build_session_context(session_key=session_key)\n",
        "store-identity-required",
    ),
    (
        "store-derived-from-a-bare-agent-name",
        "src/kiro_crew/task_planner.py",
        _LEGIT_CALL.format(store="agent"),
        "store-not-derived-from-agent",
    ),
    (
        "store-derived-from-an-agent-attribute",
        "src/kiro_crew/messaging/dispatch.py",
        _LEGIT_CALL.format(store="cfg.agents[kiro_agent].memory_store"),
        "store-not-derived-from-agent",
    ),
    (
        "store-derived-from-a-private-agent-field",
        "src/kiro_crew/subagent_manager/run.py",
        _LEGIT_CALL.format(store="self._agent"),
        "store-not-derived-from-agent",
    ),
    (
        "store-given-a-bare-literal",
        "src/kiro_crew/task_executor.py",
        _LEGIT_CALL.format(store='"default"'),
        "bare-store-literal",
    ),
    (
        # Satisfies the keyword rule and is not a string, so nothing else here
        # sees it — while ``memory_store or workspace`` resolves it to exactly
        # what passing no keyword resolves to.
        "store-given-a-literal-none",
        "src/kiro_crew/task_executor.py",
        _LEGIT_CALL.format(store="None"),
        "literal-none-store",
    ),
    (
        "target-stashed-in-a-local",
        "src/kiro_crew/dashboard/chat_runner.py",
        "builder = state.context_builder.build_message\n",
        "unanchored-target-reference",
    ),
    (
        "target-deferred-into-a-lambda",
        "src/kiro_crew/slack/gateway.py",
        "await run_in_embed_pool(lambda: self.ctx_builder.build_message)\n",
        "unanchored-target-reference",
    ),
    (
        "target-hidden-by-getattr",
        "src/kiro_crew/task_planner.py",
        'getattr(ctx, "build_message")(message, is_new, session_key)\n',
        "unanchored-target-reference",
    ),
    (
        "target-hidden-by-methodcaller",
        "src/kiro_crew/task_planner.py",
        'operator.methodcaller("build_message", message)(ctx)\n',
        "unanchored-target-reference",
    ),
    (
        "target-hidden-by-vars",
        "src/kiro_crew/task_planner.py",
        'vars(ctx)["build_message"](message)\n',
        "unanchored-target-reference",
    ),
    (
        "target-hidden-by-dunder-getattribute",
        "src/kiro_crew/task_planner.py",
        'ctx.__getattribute__("build_message")(message, is_new, session_key)\n',
        "unanchored-target-reference",
    ),
    (
        "resolver-called-with-nothing",
        "src/kiro_crew/tips.py",
        "memory = ContextBuilder.get_memory_for()\n",
        "resolver-memory-root-required",
    ),
    (
        "resolver-called-with-a-literal-none",
        "src/kiro_crew/suggestions.py",
        "memory = ContextBuilder.get_memory_for(None)\n",
        "resolver-memory-root-required",
    ),
    (
        "lesson-resolver-called-with-a-keyword-none",
        "src/kiro_crew/dashboard/handlers/_shared.py",
        "return state.context_builder.get_lessons_for(workspace=None)\n",
        "resolver-memory-root-required",
    ),
    (
        "unparseable-source",
        "src/kiro_crew/context.py",
        "def build_message(:\n",
        "unparseable-source",
    ),
    # ── allowed forms: each must produce NO hit ──
    (
        "store-passed-by-name",
        "src/kiro_crew/dashboard/chat_runner.py",
        _LEGIT_CALL.format(store="memory_store"),
        None,
    ),
    (
        "store-passed-as-the-configured-default",
        "src/kiro_crew/task_executor.py",
        _LEGIT_CALL.format(store="cfg.default_memory_store"),
        None,
    ),
    (
        "store-passed-from-resolved-bindings",
        "src/kiro_crew/messaging/dispatch.py",
        _LEGIT_CALL.format(store="bindings.memory_store_name"),
        None,
    ),
    (
        "the-definitions-themselves",
        "src/kiro_crew/context.py",
        """\
class ContextBuilder:
    def build_session_context(self, session_key, memory_store=None):
        return ""

    def build_message(self, message, is_new, session_key, memory_store=None):
        return self.build_session_context(session_key, memory_store=memory_store)
""",
        None,
    ),
    (
        "the-resolver-definitions-themselves",
        "src/kiro_crew/context.py",
        """\
@staticmethod
def get_memory_for(
    workspace: str | None = None, memory_store: str | None = None
) -> MemoryStore:
    key, store_name = _target_key(workspace, memory_store)
    return _memory_stores[key]
""",
        None,
    ),
    (
        "resolver-given-a-workspace-root",
        "src/kiro_crew/history_consolidation.py",
        "memory = ContextBuilder.get_memory_for(ws_name)\n",
        None,
    ),
    (
        "resolver-given-a-resolved-store-name",
        "src/kiro_crew/history_consolidation.py",
        "memory = ContextBuilder.get_memory_for(memory_store=store_name)\n",
        None,
    ),
    (
        # The GLOBAL store by name. Indistinguishable from a leak if it were
        # reached by omission, which is why the rule refuses the bare call.
        "resolver-given-the-default-store-by-name",
        "src/kiro_crew/tips.py",
        "memory = ContextBuilder.get_memory_for(memory_store=DEFAULT_MEMORY_STORE)\n",
        None,
    ),
    # A substring matcher would hit every one of these. They are a different
    # NAME, so an AST matcher does not.
    (
        "persistence-helper-with-a-similar-name",
        "src/kiro_crew/dashboard/chat_persistence.py",
        """\
def _build_message_entry(m: dict) -> dict | None:
    return _build_message_entry_uncached(m)
""",
        None,
    ),
    (
        "persistence-helper-referenced-without-a-call",
        "src/kiro_crew/dashboard/chat_persistence.py",
        "builder = _build_message_entry_uncached if raw else _build_message_entry\n",
        None,
    ),
    (
        "persistence-helper-spelled-as-an-attribute",
        "src/kiro_crew/history.py",
        "entry = chat_persistence._build_message_entry(m)\n",
        None,
    ),
    (
        "persistence-helper-attribute-with-no-call",
        "src/kiro_crew/dashboard/chat_backfill.py",
        "builder = chat_persistence._build_message_entry_uncached\n",
        None,
    ),
    # Prose naming the forbidden form, from both directions.
    (
        "docstring-naming-the-call",
        "src/kiro_crew/context_blocks.py",
        '"""Authoritative bounds from build_message(...) — no reconstruction."""\n',
        None,
    ),
    (
        "comment-naming-the-call",
        "src/kiro_crew/executors.py",
        "# every build_message(...) call embeds its episodic recall query\n",
        None,
    ),
    (
        "suppressed-with-a-reason",
        "src/kiro_crew/slack/gateway.py",
        "await run_in_embed_pool(  # memory-store-ok: heartbeat has no crew binding\n"
        "    self.ctx_builder.build_message, injected, is_new\n"
        ")\n",
        None,
    ),
)

# A marker with no reason must NOT suppress: (label, source, expected rule id).
_EMPTY_REASON_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "marker-with-an-empty-reason",
        "await run_in_embed_pool(  # memory-store-ok:\n"
        "    self.ctx_builder.build_message, injected, is_new\n"
        ")\n",
        "store-identity-required",
    ),
    (
        "marker-with-a-whitespace-reason",
        "await run_in_embed_pool(  # memory-store-ok:   \n"
        "    self.ctx_builder.build_message, injected, is_new\n"
        ")\n",
        "store-identity-required",
    ),
    (
        "marker-with-no-colon",
        "await run_in_embed_pool(  # memory-store-ok\n"
        "    self.ctx_builder.build_message, injected, is_new\n"
        ")\n",
        "store-identity-required",
    ),
)


def _deletion_anchor_probe() -> tuple[bool, str]:
    """Deleting a ``memory_store=`` keyword must land inside the call's scope.

    Driven through a real git repository rather than a hand-written hunk header,
    so the probe cannot pass against a stale notion of the format, and through
    :func:`hunk_touched_lines` so it covers the constant the production reader
    uses. The end state asserted is the one that matters: the anchored line falls
    inside the span of the violation the deletion created.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "r")
        os.makedirs(repo)

        def run(*args: str) -> str:
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                capture_output=True,
                check=True,
                encoding="utf-8",
                errors="replace",
            ).stdout

        run("init", "-q")
        run("config", "user.email", "probe@example.invalid")
        run("config", "user.name", "probe")
        target = os.path.join(repo, "a.py")
        declared = _LEGIT_CALL.format(store="memory_store")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(declared)
        run("add", "a.py")
        run("commit", "-qm", "base")
        base_sha = run("rev-parse", "HEAD").strip()
        # Remove ONLY the store line, leaving every neighbour byte-identical, so
        # git emits a pure `+N,0` hunk — the shape of dropping the keyword.
        stripped = "".join(
            line for line in declared.splitlines(keepends=True) if STORE_KEYWORD not in line
        )
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(stripped)
        run("add", "a.py")
        run("commit", "-qm", "drop the store")
        diff = run("diff", "--unified=0", "--no-color", base_sha, "HEAD", "--", "a.py")

    if not any(re.search(r"\+\d+,0", ln) for ln in diff.splitlines() if ln.startswith("@@")):
        return False, "the probe stopped producing a `+N,0` hunk, so it no longer tests the shape"
    anchored = hunk_touched_lines(diff)
    if not anchored:
        return False, "a deletion-only hunk reports nothing touched, so dropping a store passes"
    found = scan_source("src/kiro_crew/probe.py", stripped)
    if not found:
        return False, "the post-image of a dropped store reports no violation at all"
    if not any(v.span[0] <= n <= v.span[1] for v in found for n in anchored):
        return False, f"anchored {sorted(anchored)} misses every violation span"
    return True, ""


def self_test() -> int:
    failures = 0
    covered: set[str] = set()

    for label, path, source, expected in PROBES:
        got = {v.rule.rule_id for v in scan_source(path, source)}
        # Membership, not first-hit: one call can violate several rules at once
        # (a literal store that also names an agent), and asserting order would
        # pin the walk order, which is not part of the contract.
        ok = (expected in got) if expected else not got
        if expected:
            covered.add(expected)
        if not ok:
            print(f"  FAIL {label}: expected {expected!r}, got {sorted(got) or 'no hit'}")
            failures += 1
        else:
            print(f"  ok   {label}")

    for label, source, expected in _EMPTY_REASON_PROBES:
        got = {v.rule.rule_id for v in scan_source("src/kiro_crew/slack/gateway.py", source)}
        covered.add(expected)
        if expected not in got:
            print(f"  FAIL {label}: expected {expected!r}, got {sorted(got) or 'no hit'}")
            failures += 1
        else:
            print(f"  ok   {label}")

    ok, detail = _deletion_anchor_probe()
    if ok:
        print("  ok   deletion-only-hunk-is-anchored")
    else:
        print(f"  FAIL deletion-only-hunk-is-anchored: {detail}")
        failures += 1

    # Every rule must be exercised, or a typo that disables one ships green.
    expected_ids = {r.rule_id for r in ALL_RULES}
    missing = sorted(expected_ids - covered)
    if missing:
        print(f"  FAIL rule-coverage: no probe exercises {missing}")
        failures += 1
    else:
        print(f"  ok   rule-coverage ({len(expected_ids)} rules)")

    print("self-test passed" if not failures else f"self-test FAILED ({failures})")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def report(violations: Iterable[Violation], *, enforcing: bool, base: str | None) -> int:
    violations = list(violations)
    if not violations:
        scope = f"lines touched since {base}" if enforcing else "whole tree"
        print(f"memory-store gate: every memory-context call site names its store in the {scope}")
        return 0

    if enforcing:
        print(
            f"::error::memory-store gate: {len(violations)} memory-context call "
            f"site(s) this change touches do not name the memory store they read. A "
            f"named store is a separate SQLite silo, so an undeclared one reads "
            f"the operator's default store for a crew that configured otherwise, "
            f"and nothing goes red."
        )
    else:
        print(
            f"::notice::memory-store gate report: {len(violations)} pre-existing "
            f"call site(s) do not name their memory store. Not enforced here; only "
            f"the lines a change touches are gated, until the last site lands."
        )
    shown = 200 if enforcing else 40
    for v in violations[:shown]:
        print(v.render())
    if len(violations) > shown:
        print(f"... and {len(violations) - shown} more")
    if enforcing:
        print(
            "\nIf a rule models your call wrongly, a `# memory-store-ok: <reason>` "
            "comment on the call's opening line silences it — the reason is "
            "mandatory, and a reviewer will ask which store the call reads."
        )
    return 1 if enforcing else 0


def enforce_diff(base: str) -> int:
    """Enforce on the lines this change touches.

    A violation is in scope when its node's ``[lineno, end_lineno]`` span
    intersects the touched lines, not just when the anchor line was added: a call
    spans a dozen lines, and editing any one of them — or deleting one, which is
    how a ``memory_store=`` keyword goes away — is a change to what that call
    reads. ``unparseable-source`` ignores the scope entirely: a file whose span
    nothing could be computed for is a file nothing was checked in.
    """
    frm = diff_base(base)
    found: list[Violation] = []
    for path in changed_paths(frm):
        lines = touched_lines(frm, path)
        if not lines:
            continue
        found.extend(
            v
            for v in scan_file(path)
            if v.rule is RULE_UNPARSEABLE or any(v.span[0] <= n <= v.span[1] for n in lines)
        )
    return report(found, enforcing=True, base=base)


def force_utf8_output() -> None:
    """Print UTF-8 whatever the console's default encoding is."""
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
        return report([v for path in explicit for v in scan_file(path)], enforcing=True, base=None)

    base = os.environ.get("MEMSTORE_BASE_REF", "").strip()
    if base:
        return enforce_diff(base)

    return report(
        [v for path in tracked_files() for v in scan_file(path)], enforcing=False, base=None
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
