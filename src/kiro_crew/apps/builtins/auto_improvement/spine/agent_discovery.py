"""Agent-driven bug discovery (the model as a *discovery* source, not just a fixer).

WHY THIS EXISTS
---------------
The original bug track grounded discovery in ruff's bug-class rules: a deterministic
static signal enumerated candidate defect surfaces, and the agent only *confirmed*
reachability + authored a RED test (``profile._discover_bugs`` + ``build_gate
.discover_defect_surfaces``). That is sound but LIMITED — ruff is a linter, so it finds
linter-class footguns (naive datetime, zip-without-strict, %-format mismatches), never
the logic/contract/edge-case defects a human reviewer catches by *reading* the code:
an off-by-one, a mishandled ``None``/empty input, a swallowed exception, a TOCTOU race,
a boundary the code silently gets wrong.

This module adds the agent as a FIRST-CLASS discovery source. It asks the model to READ
the code (or, when diff-scoped, ONLY the branch's changed lines) and hypothesise concrete,
*testable* defects — each pinned to a file+line+symbol, with a one-line repro idea. It
also asks for coverage gaps: risky areas thin on tests where a reproducing test would
likely surface a latent bug. Output is the SAME surface-dict shape ruff produces
(``{target, rule, message, file, line, symbol, hypothesis?}``), so the existing
``_bug_candidate`` builder, the diff-scope filter, and the RED→GREEN→STAYGREEN gate all
work UNCHANGED downstream.

ANTI-SPECULATION DISCIPLINE (still enforced — this does NOT relax it)
--------------------------------------------------------------------
The model proposes; the deterministic gate disposes. Nothing the agent "finds" here is
trusted: every surface becomes a ``kind="bug"`` candidate whose reproducing test MUST be
RED on the base tree (twice, flake-checked) and GREEN after the fix, or it is discarded
(``no_defect``, retryable). So an over-eager or hallucinated "bug" costs at most one
bounded investigation cycle, never a fabricated fix. The agent is read-only HERE (it is
NOT allowed to edit during discovery) — the fix is authored later, in an isolated
worktree, by ``author_bug_fix``.

FOCUS BY FILE LIST, NOT A DIFF (branch→branch self-improvement)
---------------------------------------------------------------
When the run is diff-scoped (``scopeDiffBase`` set — e.g. dogfooding the app on its own
feature branch), the agent is handed the CHANGED-FILE LIST plus each file's DEPENDENTS
(callers), NOT a unified diff, and told to READ the files itself (it has Read/Grep/Glob).

WHY NOT A DIFF (operator directive 2026-06-15 — "agent should not receive diff … it should
receive file list from diff and all dependencies which are using new functionality"): a
branch that introduces a whole new subsystem produces a huge diff dominated by new-file
boilerplate (``__init__`` headers, config) that, truncated to fit context, never reaches
the logic-bearing modules — so the agent reads ``setup.cfg`` and finds nothing
(``discovered=0``). Handing it the file list + callers and letting it OPEN the actual
modules (and their dependents, to judge contract breaks) is how a human reviewer works and
is what surfaces real cross-module defects a diff-skim misses.
"""

from __future__ import annotations

import json
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

from .git_safety import GIT_SAFE_CONFIG, require_pinned

# How many agent-discovered surfaces to keep per cycle. The agent is asked for a small,
# high-signal set (not an exhaustive audit) — each surface costs a full bounded
# investigation cycle downstream, so a focused handful beats a long speculative list.
DEFAULT_AGENT_SURFACE_LIMIT = 6

# The pseudo-"rule" code tagged on an agent-discovered surface so its provenance is
# legible in the ledger/CR ("AGENT" vs a ruff code like "DTZ006"). Coverage-gap surfaces
# get COVERAGE so the two agent sub-modes are distinguishable.
RULE_AGENT_BUG = "AGENT"
RULE_AGENT_COVERAGE = "COVERAGE"

# Cap the FOCUS LIST so the agent converges fast (operator directive 2026-06-15: "shrink
# focus + hard turn cap"). A long list (82 files) made the agent investigate 10+ min and
# blow the timeout. ~12 highest-value files is enough to find real defects per cycle; the
# loop runs many cycles and the ledger dedups, so coverage accrues over runs, not in one.
DEFAULT_FOCUS_FILE_CAP = 12

# Low-value filename substrings — boilerplate/wiring with little testable logic. Dropped
# from the focus list first so the cap is spent on logic-bearing modules.
_LOW_VALUE_MARKERS = (
    "__init__.py",
    "__main__.py",
    "/config.py",
    "conftest.py",
    "/sse.py",
    "/app.py",
    "/routes.py",
    "/mcp_server.py",
    "/bridges.py",
    "/manifest",
)
# High-value substrings — pure logic likely to hold a unit-testable defect. Ranked first.
_HIGH_VALUE_MARKERS = (
    "/spine/",
    "/gate",
    "ledger",
    "keeper",
    "measur",
    "calibrat",
    "scope",
    "push_policy",
    "pr_description",
    "pr_pipeline",
    "ruler",
    "seeds",
    "selfheal",
    "normalize",
    "parse",
    "recall",
)


#: Trusted git config for host-side git over the agent-writable clone — same as the sibling
#: helpers. Discovery's `diff`/`grep`/`log` run on the HOST over the clone the agent can edit,
#: and `diff` (like `status`) consults+spawns `core.fsmonitor`; hooks run via `core.hooksPath`.
#: `-c` overrides on OUR argv beat the repo config. Part of the D-120 hook-hardening class
#: (Opus 5 review pressed on completeness across every host-side git helper). Raised by review.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG


def _git(args: list[str], cwd: Path, timeout: float = 60.0) -> str:
    """Run a read-only git command in ``cwd``; return stdout (empty on any failure).
    Discovery must never crash the loop, so every error degrades to ""."""
    require_pinned(cwd)
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *_GIT_SAFE_CONFIG, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def changed_py_files(clone: Path, base_ref: str) -> list[str]:
    """The repo-relative ``.py`` files this branch changed vs ``base_ref`` (added or
    modified), via ``git diff --name-only base...HEAD``. Empty if base is blank / git
    fails / no change. This is the FOCUS LIST handed to the agent — NOT a diff body.

    WHY a file list, not a diff: a branch that introduces a whole new subsystem produces a
    huge diff dominated by new-file boilerplate (``__init__`` headers, config) that, once
    truncated to fit context, never reaches the logic-bearing modules — so the agent reads
    setup.cfg and finds nothing. Handing it the file list and letting it READ the actual
    modules (and their callers) is what surfaces real defects (operator directive
    2026-06-15: "agent should not receive diff — it should receive file list from diff and
    all dependencies which are using new functionality")."""
    if not base_ref:
        return []
    out = _git(["diff", "--name-only", f"{base_ref}...HEAD"], clone, timeout=120.0)
    files = [ln.strip() for ln in out.splitlines() if ln.strip().endswith(".py")]
    # Drop test files from the FOCUS list — discovery targets product code (a bug in a test
    # is not a shippable defect; the agent writes its OWN reproducing test later).
    return [f for f in files if not _is_test_path(f)]


def allowlisted_py_files(clone: Path, globs: list[str]) -> list[str]:
    """Repo-relative product ``.py`` files that match the profile's EDIT-ALLOWLIST globs.

    The reading FOCUS for an UNSCOPED run (no ``scopeDiffBase``). Without it, discovery
    reads the whole tree while the edit fence confines FIXES to a subdir — so the agent
    burns its read budget on files it is mechanically forbidden to touch, finds nothing
    fixable, and returns ``[]`` every cycle. Matching the fence's own globs here means the
    agent only ever reads what a fix could actually land in. Match on both the full path and
    the basename, exactly like ``RepoEditAllowlist._matches`` (``fnmatch`` treats ``*``/``**``
    inconsistently), so this focus never diverges from the fence it mirrors. Test files are
    dropped for the same reason as ``changed_py_files``; a git-ignored / vendored tree is not
    walked (``git ls-files`` is the source of truth for what is IN the repo)."""
    if not globs:
        return []
    tracked = _git(["ls-files", "*.py"], clone, timeout=120.0)
    out: list[str] = []
    for ln in tracked.splitlines():
        rel = ln.strip()
        if not rel.endswith(".py") or _is_test_path(rel):
            continue
        name = Path(rel).name
        if any(fnmatch(rel, g) or fnmatch(name, g) for g in globs):
            out.append(rel)
    return out


def prioritize_focus(files: list[str], *, cap: int | None = None, rotate: int = 0) -> list[str]:
    """ORDER changed files by testable-logic VALUE — high-value logic modules (spine engine,
    gate, ledger, parsing, calibration…) first, boilerplate/wiring (__init__, config, routes,
    app/server) last — and return ALL of them (operator directive 2026-06-18: "do NOT limit
    the search space; if anything, randomize before any cut — I do not like cutting").

    The earlier ``cap=12`` permanently BLINDED discovery to ~69 of 81 changed files: within
    the high-value tier, paths sort alphabetically, so ``profiles/*`` + ``harness/*`` filled
    all 12 slots and the engine (``spine/driver.py``, ``backend/cr_watchers.py``, ``gate.py``,
    ``keeper.py``, …) was dropped EVERY cycle → "mined out" was an artifact of the cap, not an
    absence of bugs. So: no truncation by default (``cap=None`` returns the full ordered list).

    Within each value tier, ``rotate`` (e.g. the cycle index) deterministically ROTATES the
    order so a per-cycle read budget lands on a DIFFERENT slice of the tier each cycle —
    coverage rotates across the whole surface over the loop's many cycles instead of always
    re-reading the same alphabetical head. ``cap`` is honored only if a caller explicitly
    asks (kept for callers that want a bounded list); default is uncapped."""

    def tier(rel: str) -> int:
        p = rel.replace("\\", "/").lower()
        if any(m in p for m in _LOW_VALUE_MARKERS):
            return 2  # boilerplate/wiring — last
        if any(m in p for m in _HIGH_VALUE_MARKERS):
            return 0  # pure logic — first
        return 1  # everything else — middle

    # Stable sort by (tier, path) first, then rotate WITHIN each tier so the read budget
    # samples a different slice each cycle (deterministic given ``rotate`` — no flaky churn).
    ordered = sorted(files, key=lambda r: (tier(r), r))
    if rotate:
        out: list[str] = []
        for t in (0, 1, 2):
            grp = [r for r in ordered if tier(r) == t]
            if grp:
                k = rotate % len(grp)
                out.extend(grp[k:] + grp[:k])
        ordered = out
    return ordered[:cap] if cap else ordered


def _is_test_path(rel: str) -> bool:
    p = rel.replace("\\", "/")
    return (
        p.startswith("test/")
        or p.startswith("tests/")
        or "/test/" in p
        or "/tests/" in p
        or Path(p).name.startswith("test_")
    )


def _module_name_for(rel: str) -> str:
    """Best-effort dotted module name for a ``src/<pkg>/…/x.py`` path (for dependent search).
    ``src/kiro_crew/apps/foo/bar.py`` → ``bar`` (we grep on the leaf + a couple parents so a
    dependent ``from ...foo import bar`` or ``import bar`` is found without a full import graph)."""
    stem = Path(rel).stem
    return stem


def dependents_of(clone: Path, changed: list[str], *, limit: int = 24) -> dict[str, list[str]]:
    """Map each changed file → the OTHER source files that import/reference its module
    (the "surrounding code that uses the new functionality"). A cheap dependency probe:
    grep the src tree for the changed file's module leaf used in an ``import``/``from`` line.

    Not a precise import graph (no AST) — a deliberately cheap, dependency-direction hint so
    the agent knows which callers to read when judging whether a change broke a CONTRACT its
    users rely on. Capped so a hot module (imported everywhere) doesn't flood the prompt."""
    src_dir = Path(clone) / "src"
    if not src_dir.exists():
        return {}
    out: dict[str, list[str]] = {}
    for rel in changed:
        leaf = _module_name_for(rel)
        if not leaf or leaf in ("__init__", "__main__"):
            continue
        # Find files that import this leaf (``import leaf`` / ``from … import leaf`` /
        # ``from …leaf import``). Exclude the file itself + tests.
        res = _git_grep_imports(clone, leaf)
        deps = [f for f in res if f != rel and not _is_test_path(f)]
        if deps:
            out[rel] = deps[:6]  # a handful of representative callers per changed file
        if len(out) >= limit:
            break
    return out


def _git_grep_imports(clone: Path, leaf: str) -> list[str]:
    """Repo-relative source files that mention ``leaf`` in an import statement. Uses
    ``git grep`` (fast, respects tracked files); returns [] on any failure."""
    # Match: `import leaf`, `import leaf as`, `from x import leaf`, `from x.leaf import`
    pattern = rf"(import|from).*\b{re.escape(leaf)}\b"
    out = _git(["grep", "-lE", pattern, "--", "src/"], clone, timeout=60.0)
    return [ln.strip() for ln in out.splitlines() if ln.strip().endswith(".py")]


def _iter_json_arrays(text: str):
    """Yield every BALANCED JSON array (in source order) that parses to a ``list``.

    A bracket-matching scan that is aware of string literals (so a ``]`` inside a string
    value does not close the span). This is robust to leading/trailing bracketed PROSE
    (e.g. a trailing "see line [12]." note, or a leading "item [1]:"), which the old
    ``text.find('[') .. text.rfind(']')`` span got wrong: any stray ``[``/``]`` in prose
    corrupted the span, json.loads failed, and a perfectly good array was silently lost."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[i : j + 1])
                    except json.JSONDecodeError:
                        data = None
                    if isinstance(data, list):
                        yield data
                    break
        # Advance past this '[' (balanced or not) so a later valid array is still found.
        i += 1


def _extract_json_array(text: str) -> list[dict]:
    """Pull the first JSON array of objects out of an agent reply. The agent is asked to
    emit ONLY a JSON array, but models often wrap it in prose or a ```json fence — so we
    locate a balanced ``[ ... ]`` and parse that. Returns [] on any parse failure
    (a malformed reply yields no surfaces, never an exception)."""
    if not text:
        return []
    # Prefer a fenced block if present (```json ... ``` or ``` ... ```).
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        try:
            data = json.loads(fence.group(1))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    # Fall back to a balanced-bracket scan (string-literal aware) — prefer the first array
    # that actually contains object records, but accept the first list otherwise.
    first_list: list | None = None
    for arr in _iter_json_arrays(text):
        if first_list is None:
            first_list = arr
        if any(isinstance(d, dict) for d in arr):
            return [d for d in arr if isinstance(d, dict)]
    if first_list is not None:
        return [d for d in first_list if isinstance(d, dict)]
    return []


def _has_json_array(text: str) -> bool:
    """True iff the reply CONTAINS a parseable JSON array (even an empty ``[]``). Used to
    distinguish "the agent answered (possibly with no findings)" from "the agent never
    emitted an array" — only the latter triggers the tool-side forcing re-emit. Without
    this, a valid empty-result answer (``[]``) would wrongly trigger a second call."""
    if not text:
        return False
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        try:
            if isinstance(json.loads(fence.group(1)), list):
                return True
        except json.JSONDecodeError:
            pass
    for _arr in _iter_json_arrays(text):
        return True
    return False


def _normalize_surface(
    raw: dict,
    *,
    clone: Path,
    scope: set[str] | None,
    default_rule: str,
) -> dict | None:
    """Coerce one agent-emitted record into the canonical surface-dict shape
    ``{target, rule, message, file, line, symbol, hypothesis}`` (identical to what
    ``build_gate.discover_defect_surfaces`` returns), or None if it lacks a usable file.

    Defensive: the agent controls these strings, so we validate the file is a real
    repo-relative source path (and, when scoped, inside the change set) before trusting
    it — a hallucinated path yields a candidate whose test can't collect, wasting a cycle."""
    file_field = str(raw.get("file") or raw.get("path") or "").strip()
    if not file_field:
        return None
    # Relativize to the clone (the agent may answer with an absolute path).
    rel = file_field
    try:
        if Path(file_field).is_absolute():
            rel = str(Path(file_field).relative_to(Path(clone)))
    except ValueError:
        rel = Path(file_field).name
    rel = rel.lstrip("./")
    # Reject paths that don't exist in the clone — a fabricated surface can't be reproduced
    # and would burn a cycle as an un-collectible RED.
    if not (Path(clone) / rel).is_file():
        return None
    # When diff-scoped, drop anything outside the branch's change set (defense-in-depth;
    # the caller also re-filters, but failing here keeps the agent honest about "the diff").
    if scope is not None and rel not in scope:
        return None
    try:
        line = int(raw.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    symbol = str(raw.get("symbol") or "").strip() or (f"L{line}" if line else default_rule)
    message = str(raw.get("message") or raw.get("title") or "").strip()
    hypothesis = str(raw.get("hypothesis") or raw.get("repro") or "").strip()
    rule = str(raw.get("rule") or default_rule).strip() or default_rule
    target = f"{rel}::{symbol}"
    return {
        "target": target,
        "rule": rule,
        "message": message,
        "file": rel,
        "line": line,
        "symbol": symbol,
        "hypothesis": hypothesis,
    }


# How many files form THIS cycle's PRIORITY SLICE — the bounded set the agent is told to
# actually read this cycle so it converges within the turn budget. The FULL changed-file list
# stays VISIBLE below it (operator 2026-06-18: do not limit the search space) — nothing is
# hidden; the slice just rotates each cycle (upstream rotate=), so over the loop's cycles the
# read budget sweeps the ENTIRE surface. This replaces the old hard cap=12 that PERMANENTLY
# dropped 69 files: here all 82 are listed, only the per-cycle *reading focus* is bounded.
DEFAULT_PRIORITY_SLICE = 12


def _render_focus_list(
    changed: list[str], dependents: dict[str, list[str]], *, slice_n: int = DEFAULT_PRIORITY_SLICE
) -> str:
    """Render the changed-file list as a PRIORITY SLICE (this cycle's reading focus, bounded
    so the agent converges within its turn budget) followed by the REST (still fully visible —
    no cut — for later rotated cycles). ``changed`` is already value-ordered + cycle-rotated
    upstream, so the slice is the most-promising rotated head. NO diff body — just paths."""

    def fmt(rel: str) -> str:
        deps = dependents.get(rel) or []
        return f"  - {rel}   (used by: {', '.join(deps)})" if deps else f"  - {rel}"

    if not slice_n or len(changed) <= slice_n:
        return "\n".join(fmt(r) for r in changed)
    head, rest = changed[:slice_n], changed[slice_n:]
    lines = ["PRIORITY SLICE — read FROM THESE this cycle (the rotated most-promising files):"]
    lines += [fmt(r) for r in head]
    lines.append("")
    lines.append(
        f"ALSO CHANGED ({len(rest)} more — the full surface; a LATER cycle rotates these into "
        "the priority slice, so you need NOT read them now — only dip in if the slice is clean):"
    )
    lines += [fmt(r) for r in rest]
    return "\n".join(lines)


# Cap the skip-list rendered into the prompt so a long ledger doesn't blow the context
# budget. The most recent terminal loci matter most; the downstream dedup is the real
# guarantee — this is a COST optimization (don't waste reads), not a correctness gate.
DEFAULT_SKIP_LIST_CAP = 40


def _build_prompt(
    *,
    src_label: str,
    changed: list[str],
    dependents: dict[str, list[str]],
    scoped: bool,
    limit: int,
    skip_targets: list[str] | None = None,
    allowlist_focus: bool = False,
) -> str:
    """The discovery prompt. Read-only investigation; STRICT JSON-array output.

    KEY DESIGN (operator directive 2026-06-15): the agent is NOT handed a unified diff — a
    raw diff pollutes context (new-subsystem branches are dominated by boilerplate that
    truncates before the logic) and led to discovered=0. Instead it gets the CHANGED-FILE
    LIST plus each file's DEPENDENTS (callers), and is told to READ the files itself (it has
    Read/Grep/Glob) — open the changed modules AND their callers, and judge whether the code
    is correct and whether it honors the contract its callers rely on. This is how a human
    reviewer works, and is what surfaces real cross-module defects a diff-skim misses."""
    if allowlist_focus and changed:
        focus = (
            "The files below are the ONLY region a fix may land in (the edit fence confines "
            "every change to these paths — a defect ANYWHERE ELSE cannot be fixed by this loop, "
            "so do NOT report it). They are ordered most-promising-first and rotate each cycle. "
            "For each, the files that USE it (its callers/dependents) are in parentheses.\n\n"
            "BUDGET — read this FIRST: read ONLY from the PRIORITY SLICE below this cycle "
            "(~12 files), open UP TO 8 of them, ONE read each; do NOT re-read a file. The moment "
            "you have 2-3 solid findings OR have used 8 reads (whichever comes FIRST), STOP and "
            "reply with ONLY the JSON array. Reading is INSTRUMENTAL — it is NOT the task. An "
            "answer with 2 findings is SUCCESS; reading until you time out with no answer is "
            "TOTAL FAILURE. NEVER end your turn on a tool call — your FINAL message MUST be the "
            "JSON array.\n\n"
            "METHOD: start at the TOP of the PRIORITY SLICE (highest-value logic this cycle — "
            "engine modules, gate/ledger/keeper, parsing, math/ordering; SKIP __init__/"
            "__main__/config/wiring), read promising files ONCE, and the moment you spot a "
            "testable defect, record it. You MAY read a caller/dependent OUTSIDE this list to "
            "judge a contract break, but the DEFECT you report must be IN a listed file. "
            "Then EMIT.\n\n"
            f"{_render_focus_list(changed, dependents)}\n"
        )
    elif scoped and changed:
        focus = (
            "This branch introduces/changes the files below (the FOCUS LIST — the COMPLETE "
            "changed-file surface, ordered most-promising-first; it rotates each cycle so "
            "different files lead on different cycles). For each, the files that USE it (its "
            "callers/dependents) are in parentheses.\n\n"
            "BUDGET — read this FIRST: read ONLY from the PRIORITY SLICE below this cycle "
            "(~12 files), open UP TO 8 of them, ONE read each; do NOT re-read a file. The full "
            "surface is listed under 'ALSO CHANGED' for context, but a LATER cycle rotates "
            "those into the slice — you do NOT need to read them now. The moment you have 2-3 "
            "solid findings OR have used 8 reads (whichever comes FIRST), STOP and reply with "
            "ONLY the JSON array. Reading is INSTRUMENTAL — it is NOT the task. An answer with "
            "2 findings is SUCCESS; reading until you time out with no answer is TOTAL FAILURE. "
            "NEVER end your turn on a tool call — your FINAL message MUST be the JSON array.\n\n"
            "METHOD: start at the TOP of the PRIORITY SLICE (highest-value logic this cycle — "
            "engine modules, gate/ledger/keeper, parsing, math/ordering; SKIP __init__/"
            "__main__/config/wiring), read promising files ONCE, and the moment you spot a "
            "testable defect, record it. Favor contract breaks (a function returns a shape/"
            "status/0-value its callers don't expect). Then EMIT.\n\n"
            f"{_render_focus_list(changed, dependents)}\n"
        )
    else:
        focus = (
            "You are reviewing a Python codebase. READ the code under "
            f"{src_label} and find concrete, TESTABLE logic defects a careful reviewer would "
            "catch: off-by-one, mishandled None/empty input, swallowed exceptions, wrong "
            "boundary conditions, incorrect error handling, races, contract violations.\n"
        )
    # SKIP-LIST: loci already terminal in the ledger (filed/committed/failed_gate/duplicate
    # …). Re-proposing them wastes a full investigation read + is dropped downstream by the
    # dedup gate anyway, so tell the agent up front NOT to report them — it spends its 6-read
    # budget on genuinely NEW surfaces (operator: discovery re-emits already-terminal
    # candidates every cycle). Capped so a long ledger can't blow the prompt budget.
    skip_block = ""
    if skip_targets:
        shown = skip_targets[:DEFAULT_SKIP_LIST_CAP]
        more = len(skip_targets) - len(shown)
        skip_block = (
            "ALREADY HANDLED — do NOT report these loci (already filed/fixed/triaged this "
            "project; re-reporting them is wasted effort and will be discarded):\n"
            + "\n".join(f"  - {t}" for t in shown)
            + (f"\n  …and {more} more already-handled loci.\n" if more > 0 else "\n")
            + "Spend your read budget on surfaces NOT in this list.\n\n"
        )
    return (
        "You are the DISCOVERY step of an autonomous bug-finding loop. Your job is to "
        "find REAL, REPRODUCIBLE defects — NOT style, NOT linter nits, NOT speculation.\n\n"
        f"{focus}\n"
        f"{skip_block}"
        "WHAT COUNTS (must be TESTABLE — you can write a unit test that FAILS on current code):\n"
        "  • wrong comparison/boundary/off-by-one; mishandled None/empty (max() on empty, [0] on "
        "maybe-empty, KeyError); swallowed exception hiding a failure; wrong value on an error "
        "path; a CONTRACT VIOLATION (function returns a shape/status its callers don't expect — "
        "e.g. a hard-coded status where the caller's value should flow through); a falsy-zero bug "
        '(`x or default` treating a valid 0/""/[] as missing); mutable default arg; format/encoding bug.\n\n'
        "Also flag up to a couple of COVERAGE GAPS: a self-contained function with non-trivial "
        "logic and little/no test coverage where a focused test would likely surface a latent "
        'bug. Mark these "rule": "COVERAGE".\n\n'
        "DISCIPLINE — critical:\n"
        "  • Every item MUST be something you could write a unit test for that FAILS on the "
        "current code, naming the EXACT input and the WRONG output. If you can't, DO NOT report it.\n"
        "  • Prefer self-contained modules (pure helpers, parsing, date/time, config, data "
        "transforms, the spine engine logic) — their bugs are unit-testable. AVOID "
        "network/server/route/handler/MCP-wiring code: a reproducing test for those can't import "
        "standalone in a bare clone and is worthless to this loop.\n"
        "  • Pin each to an EXACT file (repo-relative), the line number, and the function/symbol.\n"
        "  • Quality over quantity. Return AT MOST "
        f"{limit} items — the highest-signal ones. Fewer is better than padded.\n\n"
        "You may READ and SEARCH (Read, Grep, Glob). There is NO shell: discovery is "
        "read-only by CAPABILITY, not by instruction, and the fix is authored later.\n\n"
        "OUTPUT: reply with ONLY a JSON array (no prose, no markdown fence), each element:\n"
        "  {\n"
        '    "file":       "<repo-relative path, e.g. src/kiro_crew/foo/bar.py>",\n'
        '    "line":       <int line number of the defect>,\n'
        '    "symbol":     "<function or class name>",\n'
        '    "rule":       "AGENT"  (or "COVERAGE" for a coverage-gap item),\n'
        '    "message":    "<one-line description of the defect>",\n'
        '    "hypothesis": "<one sentence: the input and the wrong output a unit test would assert>"\n'
        "  }\n"
        "If you find nothing genuinely testable, reply with exactly: []"
    )


def _diag_log(log_dir: Path | None, payload: dict) -> None:
    """Append one JSON diagnostic record to ``<log_dir>/agent_discovery.log`` (best-effort,
    never raises). This is the durable visibility into WHY discovery returned what it did —
    the full prompt, the raw agent reply, every dropped surface + its reason, and the final
    count. Without it, a ``discovered=0`` is opaque (operator directive 2026-06-15:
    "make sure sufficient logs are produced for further diagnostics")."""
    if log_dir is None:
        return
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, default=str)
        with open(log_dir / "agent_discovery.log", "a") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — logging must never break discovery
        pass


def discover_surfaces_via_agent(
    runner: Any,
    *,
    clone: Path,
    scope_base: str = "",
    scope: set[str] | None = None,
    limit: int = DEFAULT_AGENT_SURFACE_LIMIT,
    testability_rank: Callable[[str], Any] | None = None,
    timeout_s: float = 720.0,
    log_dir: Path | None = None,
    logger: Any = None,
    skip_targets: list[str] | None = None,
    rotate: int = 0,
    edit_globs: list[str] | None = None,
) -> list[dict]:
    """Run the agent as a discovery source and return defect/coverage surface dicts.

    ``rotate`` (e.g. the cycle index) rotates the focus ordering WITHIN each value tier so a
    per-cycle read budget samples a different slice of the FULL changed-file surface each
    cycle — coverage rotates across all files over the loop's cycles (operator directive
    2026-06-18: do not limit the search space; rotate rather than cut).

    Same return shape as ``build_gate.discover_defect_surfaces`` so the caller treats both
    sources identically: ``{target, rule, message, file, line, symbol, hypothesis}``.

    Args:
      runner: an AgentRunner / SessionAgentRunner (anything with ``.run(prompt, ...)``
        returning an ``AgentResult``). If None / unavailable, returns [] (offline → no
        agent discovery, ruff still runs).
      clone: the push-disabled working clone the agent reads (cwd for the agent).
      scope_base: the ``scopeDiffBase`` ref; when set, the agent is focused on the diff.
      scope: the resolved in-scope relpath set (defense-in-depth re-filter); None = unscoped.
      limit: max surfaces to keep.
      testability_rank: optional ``rel_path -> sortable`` key (the profile's own ranker)
        so author-first ordering matches the ruff path; falls back to insertion order.
      log_dir: if set, a JSON diagnostic record (prompt, raw reply, dropped surfaces +
        reasons, counts) is appended to ``<log_dir>/agent_discovery.log`` for diagnostics.
      logger: optional stdlib logger; a one-line INFO summary is emitted (counts + why).

    Never raises — discovery must not crash the loop. Read-only (the agent cannot edit)."""

    def _log(msg: str) -> None:
        if logger is not None:
            try:
                logger.info("agent-discovery: %s", msg)
            except Exception:  # noqa: BLE001
                pass

    if runner is None:
        _log("no agent runner wired → 0 surfaces (offline)")
        return []
    run = getattr(runner, "run", None)
    if not callable(run):
        _log("runner has no .run() → 0 surfaces")
        return []
    scoped = bool(scope_base)
    # FOCUS LIST instead of a diff body: the changed product files + their callers. The agent
    # READS the files itself (operator directive 2026-06-15). If a scope was requested but
    # produced no changed files (blank base / git failure / empty change set), fall back to a
    # whole-tree read so the agent still has something to do — mirrors how _diff_scope degrades
    # to unscoped rather than narrowing to zero.
    all_changed = changed_py_files(Path(clone), scope_base) if scoped else []
    # UNSCOPED + an edit allowlist: read ONLY the fixable region. The edit fence confines a
    # FIX to ``edit_globs`` (e.g. one subdir), so reading the whole tree makes the agent spend
    # its budget on files it is mechanically forbidden to touch — it finds nothing fixable and
    # returns [] every cycle (observed dogfooding the app on its own subtree). Focusing reads
    # on the allowlisted files means everything it surfaces is a candidate a fix can land in.
    # A diff scope is more specific, so it still wins; this only fills the unscoped case.
    allowlist_focus = False
    if not scoped and edit_globs:
        allowlisted = allowlisted_py_files(Path(clone), edit_globs)
        if allowlisted:
            all_changed = allowlisted
            allowlist_focus = True
    # ORDER the FULL set high-value-logic-first, then ROTATE within each tier by the cycle
    # index so the read budget samples a different slice each cycle (operator directive
    # 2026-06-18: do NOT limit the search space — the old cap=12 permanently hid the engine
    # modules behind alphabetically-earlier profiles/ files). NO truncation: the agent sees
    # every focus file (rendered compactly as a priority slice + the rest).
    changed = prioritize_focus(all_changed, rotate=rotate)
    dependents = dependents_of(Path(clone), changed) if changed else {}
    src_dir = Path(clone) / "src" / "kiro_crew"
    src_label = "src/kiro_crew" if src_dir.exists() else "src"
    prompt = _build_prompt(
        src_label=src_label,
        changed=changed,
        dependents=dependents,
        scoped=scoped and bool(changed),
        limit=limit,
        skip_targets=skip_targets,
        allowlist_focus=allowlist_focus,
    )
    _log(
        f"start: scoped={scoped} allowlist_focus={allowlist_focus} "
        f"changed_total={len(all_changed)} focus={len(changed)} "
        f"dependents_mapped={len(dependents)} skip_list={len(skip_targets or [])} "
        f"prompt_chars={len(prompt)}"
    )
    err = ""
    try:
        res = run(
            prompt,
            cwd=str(clone),
            # NO shell. The comment here used to read "read-only investigation" while granting
            # `Bash`, which is write-capable — and this agent runs in the SHARED clone, the
            # tree the loop later stages and commits from. Discovery's whole job is to READ
            # the target repository's source, which is untrusted content, so an injection
            # there could have edited that tree and a later `git add -A` would publish an edit
            # no measurement gated. `allowed_tools` also AUTO-APPROVES, so such a call never
            # reaches the platform governance chokepoint. Read/Grep/Glob cover everything the
            # prompt actually asks for. Raised by the GPT review.
            allowed_tools=["Read", "Grep", "Glob"],
            # HARD turn cap — the convergence lever (validated 2026-06-16). A thinking/opus
            # agent has NO terminal commitment, so the cap must MATCH the reading surface: the
            # full 82-file list with a 12-turn cap was exhausted by reading before the agent
            # emitted → runner_error=max_turns, raw_items=0 EVERY cycle. The fix keeps the FULL
            # list VISIBLE (operator 2026-06-18: do not limit the search space) but bounds the
            # per-cycle READING to a rotated PRIORITY SLICE (~12 files, _render_focus_list), so
            # the agent only needs to investigate the slice — which fits a modest turn budget.
            # 16 turns: ~12-slice + a few emit/think turns, with margin over the old 12. The
            # runner returns accumulated text on the limit so a late JSON array survives;
            # tool-side forcing drops Read/Grep on the final turns as the backstop.
            max_turns=16,
            timeout_s=timeout_s,
        )
    except Exception as e:  # noqa: BLE001 — never crash the loop on a discovery agent error
        _log(f"runner raised {type(e).__name__}: {e}")
        _diag_log(
            log_dir,
            {
                "phase": "error",
                "scoped": scoped,
                "changed_files": len(changed),
                "error": f"{type(e).__name__}: {e}",
                "prompt": prompt,
            },
        )
        return []
    text = getattr(res, "text", "") or ""
    ok = getattr(res, "ok", None)
    err = getattr(res, "error", "") or ""
    raw_items = _extract_json_array(text)
    # TOOL-SIDE FORCING FALLBACK (validated 2026-06-16 — the most robust convergence fix):
    # if the investigation pass produced NO parseable JSON (the agent over-investigated and
    # got cut off mid-reading — runner not-ok, or ok but no array), make ONE more call with
    # NO tools, handing back the agent's own reasoning and demanding ONLY the JSON array.
    # With no Read/Grep available, the only possible action is to answer — converting a
    # "read until timeout" run into the findings it already reasoned about. This is the
    # harness forcing function the subagent comparison identified: prompt wording alone
    # doesn't beat a thinking agent's investigation momentum; removing the tools does.
    forced = False
    # Trigger the re-emit ONLY when the agent produced NO json array at all (over-investigated
    # and got cut off) — NOT when it answered with a valid empty array `[]` (a legitimate
    # "no findings" result, which must be respected without a second call).
    if not raw_items and not _has_json_array(text) and text.strip():
        forced = True
        emit_prompt = (
            "You investigated a codebase for defects but did NOT finish with the required "
            "JSON output. Below is YOUR OWN analysis so far. Do NOT read any more files — "
            "you have NO tools now. Based ONLY on what you already found, output the JSON "
            "array of defects NOW (the exact schema you were given: file, line, symbol, "
            "rule, message, hypothesis). If your analysis surfaced no concrete testable "
            "defect, output exactly []. Output ONLY the JSON array, nothing else.\n\n"
            "=== YOUR ANALYSIS SO FAR ===\n" + text[-8000:]
        )
        try:
            res2 = run(
                emit_prompt,
                cwd=str(clone),
                allowed_tools=[],  # NO tools → must answer
                max_turns=1,
                timeout_s=120.0,
            )
            text2 = getattr(res2, "text", "") or ""
            items2 = _extract_json_array(text2)
            _log(f"tool-side forcing: re-emit produced {len(items2)} item(s)")
            if items2:
                raw_items = items2
                text = text + "\n\n[FORCED-EMIT]\n" + text2
        except Exception as e:  # noqa: BLE001
            _log(f"forced re-emit failed: {type(e).__name__}")
    # Parse + record EVERY raw item with its keep/drop reason, so a 0-result is explainable.
    surfaces: list[dict] = []
    seen: set[str] = set()
    dropped: list[dict] = []
    for raw in raw_items:
        default_rule = (
            RULE_AGENT_COVERAGE
            if str(raw.get("rule", "")).upper().startswith("COV")
            else RULE_AGENT_BUG
        )
        s = _normalize_surface(
            raw,
            clone=Path(clone),
            scope=scope,
            default_rule=default_rule,
        )
        if s is None:
            dropped.append({"raw": raw, "reason": "invalid/nonexistent file or out-of-scope"})
            continue
        if s["target"] in seen:
            dropped.append({"raw": raw, "reason": "duplicate target"})
            continue
        seen.add(s["target"])
        surfaces.append(s)
    # Author-first ordering (self-contained leaves before framework/network files) so the
    # cycles spend on candidates whose reproducing test can actually collect + go RED.
    if testability_rank is not None:
        try:
            surfaces.sort(key=lambda s: testability_rank(s["file"]))
        except Exception:  # noqa: BLE001 — ordering is a hint; never fail on it
            pass
    surfaces = surfaces[:limit]
    # ── durable diagnostic record (the WHY behind the count) ──
    _diag_log(
        log_dir,
        {
            "phase": "result",
            "scoped": scoped,
            "changed_files": len(changed),
            "dependents_mapped": len(dependents),
            "prompt_chars": len(prompt),
            "runner_ok": ok,
            "runner_error": err,
            "reply_chars": len(text),
            "forced_emit": forced,
            "raw_items": len(raw_items),
            "kept": len(surfaces),
            "dropped": dropped,
            "surfaces": surfaces,
            "prompt": prompt,
            "reply": text,
        },
    )
    _log(
        f"done: ok={ok} reply_chars={len(text)} raw_items={len(raw_items)} "
        f"kept={len(surfaces)} dropped={len(dropped)}"
        + (f" runner_error={err[:120]}" if err else "")
    )
    return surfaces
