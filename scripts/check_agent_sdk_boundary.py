#!/usr/bin/env python3
"""check_agent_sdk_boundary.py -- application code must not import the ACP layer.

## The failure class

`kiro_crew.providers` is positioned as the boundary between application code and
the agent backend, and is not one. It re-exports ACP symbols rather than
translating them -- `providers/base.py` aliases `kiro_crew.acp.types.AcpEvent` as
`LLMEvent`, so the "provider-agnostic" event type IS the ACP dataclass, complete
with a raw JSON-RPC `request_id` -- and dozens of modules skip it and import
`kiro_crew.acp` directly, several reaching underscore-private names. The
consequence is that switching agent backends is not a driver swap; it is an edit
across the whole tree.

`docs/request-for-change/rfc-crew-agent-sdk-boundary.md` proposes
`kiro_crew.agent_sdk` as the single import surface, with `kiro_crew.acp` private
to one driver behind it. This gate is that RFC's Phase 1: it does not move any
code, it records today's leak and refuses to let it grow.

## What counts as a violation

An import, in a file under `src/` that is NOT part of the boundary itself, whose
target resolves to `kiro_crew.acp*` or `kiro_crew.providers*`.

Both targets are forbidden, and the second one is the point. A gate that watched
only `kiro_crew.acp` would report progress that is not real: `providers` is a
legal laundering channel for exactly the vocabulary the boundary exists to
contain (`LLMEvent` and the `EVENT_*` constants), so a
consumer could be "migrated" off `acp` while still reading ACP shapes, and the
count would fall. Watching both means the number only drops when a consumer
stops depending on the backend at all.

Three source trees are exempt, because they ARE the boundary:

* `src/kiro_crew/agent_sdk/` -- the SDK and its driver, the one place allowed to
  reach the ACP layer.
* `src/kiro_crew/acp/` -- itself.
* `src/kiro_crew/providers/` -- the shim being retired. **This exemption is
  temporary**: the RFC's final phase deletes the package, and when it goes this
  entry should go with it.

`test/` is out of scope on purpose. A test that exercises the driver must import
the driver's own dependencies, and gating that would make the boundary
untestable.

Matching is AST-based, so a multi-line import is judged as one statement and a
`if TYPE_CHECKING:` import is still a violation -- a type-only dependency is
still boundary knowledge, and it is how a consumer keeps hold of an ACP class
after the runtime call is gone. `importlib.import_module("kiro_crew.acp...")`
and `__import__` with a literal argument are caught for the same reason. A file
that does not parse is a hard ERROR, never "clean": under a shrink-only ratchet
a parse failure that read as zero violations would invite a prune that deletes
the file's real entry.

## No opt-out marker, deliberately

Sibling gates in this repository offer an inline marker for a site that is
correct-by-intent. This one does not. "This consumer legitimately needs the ACP
layer" is precisely the claim the boundary exists to refuse, so a marker would
be the hole rather than the audit trail. The baseline below is the only
exemption mechanism, and it can only shrink.

## The ratchet

Existing edges are recorded in `.github/agent-sdk-boundary-baseline.txt` as
`<count> <path>` lines. The rules mirror `check_subprocess_encoding.py` and
`check_black_formatting.py` (same problem: a large pre-existing violation set
that must only shrink):

* a file NOT in the baseline must be clean;
* a baselined file may not grow its count;
* in a file this change touches, an edge sitting on an ADDED line is a new
  offender even when the count is level -- otherwise deleting one old import
  while adding one new one would slip through unchanged;
* a baselined file whose count has shrunk (or that is clean or gone) must be
  pruned so the list only shrinks -- run `--update-baseline`, which only ever
  lowers counts and deletes lines, never adds or raises one.

The "new offender" and "prune" verdicts are scoped to the files this change
touches, for the reason the sibling gates document: CI evaluates a merge ref, so
an unscoped gate would redden a PR for files the base branch merged after the
baseline was recorded, and a stale count caused by someone else's cleanup is
their prune to record.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / ".github" / "agent-sdk-boundary-baseline.txt"

# Only shipped runtime code. See the module docstring for why test/ is excluded.
DEFAULT_TARGETS = ("src",)

# Import targets application code may not reach.
FORBIDDEN_ROOTS = ("kiro_crew.acp", "kiro_crew.providers")

# Source trees that ARE the boundary: the SDK plus the two packages it is built
# on. All three stay. ACP is the SDK's foundation rather than an implementation
# detail being sealed off, so providers/ is not scheduled for deletion.
EXEMPT_PREFIXES = (
    "src/kiro_crew/agent_sdk/",
    "src/kiro_crew/acp/",
    "src/kiro_crew/providers/",
)

DYNAMIC_IMPORTERS = ("import_module", "__import__")

HEADER = """\
# Imports of the ACP layer from application code, as `<count> <path>`.
# Both `kiro_crew.acp` and `kiro_crew.providers` count: providers re-exports ACP
# shapes (LLMEvent and the EVENT_* constants), so watching only `acp` would let
# a consumer look migrated while still reading the backend's own types.
#
# The gate requires every OTHER file under src/ to be clean and none of these
# counts to grow, so the list can shrink but never grow.
#
# It is a floor, not a countdown to zero. Most of these consumers are expected
# to keep reading ACP directly: ACP is the foundation the SDK is built on, not
# a layer being hidden behind it. What the gate buys is that the dependency
# cannot SPREAD into a file that is clean today -- which is what makes it safe
# to consolidate provider-specific logic gradually instead of in one sweep.
#
# There is no inline opt-out marker: "this consumer legitimately needs ACP" is
# the claim the boundary refuses, so the baseline is the only exemption
# mechanism.
#
# Do NOT add or raise a line to make a red gate green -- route the dependency
# through kiro_crew.agent_sdk instead. Design of record:
# docs/request-for-change/rfc-crew-agent-sdk-boundary.md
#
# Refresh (after removing something listed here):
#   python3 scripts/check_agent_sdk_boundary.py --update-baseline
"""


def _load_scope():
    """The shared diff-scope helpers (see scripts/ratchet_scope.py).

    Loaded by path, not imported: ``scripts/`` is not a package, so a plain
    import would resolve only by accident of ``sys.path[0]`` -- and not at all
    when a test loads this gate by path. Every baselined gate needs the identical
    answers to "which files does THIS change touch" and "which lines did it add",
    and the module owns both so no gate has to reach into another gate's private
    functions to get them.
    """
    script = ROOT / "scripts" / "ratchet_scope.py"
    spec = importlib.util.spec_from_file_location("ratchet_scope", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_exempt(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in EXEMPT_PREFIXES)


def _forbidden(dotted: str) -> bool:
    """True when a dotted module path lands inside a forbidden root."""
    return any(dotted == root or dotted.startswith(root + ".") for root in FORBIDDEN_ROOTS)


def _absolute_from_relative(rel_path: str, level: int, module: str | None) -> str:
    """Resolve `from ..acp.types import x` to its dotted absolute form.

    `rel_path` is repo-relative and starts with `src/`. A package's own path is
    its directory; a module's is its parent directory. Level 1 addresses that
    package, and each extra level strips one more component.
    """
    parts = list(Path(rel_path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    # Dropping the last component gives the containing package either way:
    # a module loses its own name, `__init__` loses the marker.
    package = parts[:-1]
    ascend = max(level - 1, 0)
    base = package[: len(package) - ascend] if ascend <= len(package) else []
    if module:
        base = base + [module]
    return ".".join(base)


def _matched_root(dotted: str) -> str | None:
    """The forbidden root a dotted path lands in, longest match first."""
    for root in sorted(FORBIDDEN_ROOTS, key=len, reverse=True):
        if dotted == root or dotted.startswith(root + "."):
            return root
    return None


def _str_const(node: ast.AST | None) -> str | None:
    """The value of a string literal, or None for anything not statically known."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _positional(call: ast.Call, index: int) -> ast.AST | None:
    return call.args[index] if len(call.args) > index else None


def _dynamic_importer_names(tree: ast.AST) -> set[str]:
    """Every local name that refers to `import_module` or `__import__`.

    The bare spellings are always in scope, but an importer can be renamed --
    `from importlib import import_module as _im`, or `_im = importlib.import_module`
    -- and a gate that matches only the canonical name misses the call entirely.
    Collected in one pre-pass so the order of definition and use does not matter.

    The canonical names stay in the set unconditionally, so a module that defines
    its own `def import_module(...)` and passes it a `kiro_crew.acp.*` string is
    flagged. That is deliberate: no such definition exists in this tree, and the
    gate must prefer a visible false red -- which a reader corrects -- over a
    silent false green, the same reason a file that fails to parse is a hard error
    rather than "clean".
    """
    names = set(DYNAMIC_IMPORTERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in DYNAMIC_IMPORTERS:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            value = node.value
            referenced = (
                value.attr
                if isinstance(value, ast.Attribute)
                else value.id if isinstance(value, ast.Name) else ""
            )
            if referenced in DYNAMIC_IMPORTERS:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _is_ancestor_of_forbidden(dotted: str) -> bool:
    """True when a forbidden root lives strictly under `dotted`.

    `kiro_crew` is an ancestor of `kiro_crew.acp`; `kiro_crew.sandbox` is not.
    This is what decides whether a `fromlist` can reach the boundary at all.
    """
    return any(root.startswith(f"{dotted}.") for root in FORBIDDEN_ROOTS)


def _literal_strings(node: ast.AST) -> tuple[list[str], bool]:
    """The strings an iterable literal yields, and whether that list is COMPLETE.

    The second value is the whole point. Three rounds of review on this function
    each closed one container shape and revealed the next -- tuple, list, set,
    then dict keys, then starred unpacking, dict-unpack and literal concatenation
    -- because enumerating shapes is a game the reader of the code cannot win:
    `('acp',) * 1`, `('a' + 'cp',)`, `frozenset({'acp'})` and a comprehension all
    import the same package. So the question asked here is inverted. Not "can I
    find a forbidden name in this expression?" but "can I prove this expression
    contains none?" -- and anything not fully understood answers no.
    """
    if isinstance(node, ast.Constant):
        # A bare string is iterated CHARACTER by character, so it can only ever
        # name single-letter submodules -- decidable, and never a forbidden root.
        if isinstance(node.value, str):
            return list(node.value), True
        return [], True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elements = list(node.elts)
    elif isinstance(node, ast.Dict):
        # A `**` unpack has a None key: the keys it contributes are unknown.
        if any(key is None for key in node.keys):
            return [], False
        elements = [key for key in node.keys if key is not None]
    else:
        return [], False

    found: list[str] = []
    for element in elements:
        if isinstance(element, ast.Constant):
            if isinstance(element.value, str):
                found.append(element.value)
            continue
        # Starred, BinOp, Call, Name, comprehension, f-string: not decidable.
        return found, False
    return found, True


def _dynamic_import_targets(rel: str, call: ast.Call) -> list[str]:
    """Dotted targets a dynamic-import call reaches, across every argument form.

    `import_module(name, package=None)` and
    `__import__(name, globals, locals, fromlist=(), level=0)` each carry the module
    in more than one place, and a gate that reads only the first positional
    argument is bypassed by every other spelling:

    - `name` may be a keyword (`import_module(name="kiro_crew.acp.client")`).
    - `fromlist` holds the real target when `name` is only the package
      (`__import__("kiro_crew", fromlist=("acp",))` imports `kiro_crew.acp`).
    - a leading-dot `name` plus a `package` is relative, in either slot
      (`import_module(".acp", package="kiro_crew")` and the positional
      `import_module(".acp", "kiro_crew")`).
    - `level` > 0 is relative to the calling module's own package
      (`__import__("acp", level=1)`).

    A non-literal argument is deliberately NOT resolved: `mod = cfg.name;
    import_module(mod)` cannot be decided statically, and guessing would either
    miss it anyway or flag innocent code. That gap is recorded as a clean probe in
    the self-test so it stays a known limit rather than an assumed capability.
    """
    # `or` would be wrong here: `_str_const` returns "" for `__import__("", ...)`,
    # which is FALSY but a perfectly legal module name -- it is how a purely
    # relative import spells itself, and `__import__("", globals(), locals(),
    # ("acp",), 2)` really does import `kiro_crew.acp`. Chaining with `or` treated
    # that as "no name given" and returned early, so the whole relative branch
    # below never ran. Test for None, not for truth.
    name = _str_const(_positional(call, 0))
    if name is None:
        name = _str_const(_kwarg(call, "name"))
    if name is None:
        return []

    level_node = _positional(call, 4) or _kwarg(call, "level")
    level = (
        level_node.value
        if isinstance(level_node, ast.Constant) and isinstance(level_node.value, int)
        else 0
    )
    # `package` is argument 1 of `import_module(name, package=None)`, so reading
    # only the keyword left the positional spelling `import_module(".acp",
    # "kiro_crew")` with package=None. The relative branch then fell back to
    # resolving against the CALLING file's package, and for any nested consumer
    # that lands on a non-forbidden target (`dashboard/foo.py` ->
    # `kiro_crew.dashboard.acp`) -- a false green, in the one branch whose whole
    # claim is that it closes every spelling. Slot 1 is `globals` for
    # `__import__`, but a dict literal is not a string constant, so reading it
    # here cannot produce a name.
    package = _str_const(_kwarg(call, "package"))
    if package is None:
        package = _str_const(_positional(call, 1))

    if name.startswith("."):
        dots = len(name) - len(name.lstrip("."))
        remainder = name[dots:]
        if package:
            base = package if dots <= 1 else ".".join(package.split(".")[: -(dots - 1)])
            name = f"{base}.{remainder}" if remainder else base
        else:
            name = _absolute_from_relative(rel, dots, remainder or None)
    elif level:
        name = _absolute_from_relative(rel, level, name)

    targets = [name]
    fromlist = _positional(call, 3) or _kwarg(call, "fromlist")
    if fromlist is not None and _is_ancestor_of_forbidden(name):
        # `name` alone is innocent, so whether this call crosses the boundary is
        # decided entirely by `fromlist`. Clean requires PROOF that it names no
        # forbidden package; an expression this scanner cannot fully read is
        # reported, because the alternative is a silent green on a real import.
        # See `_literal_strings` for why the question is posed that way round.
        items, decidable = _literal_strings(fromlist)
        if decidable:
            targets.extend(f"{name}.{item}" for item in items if item != "*")
        else:
            targets.extend(root for root in FORBIDDEN_ROOTS if root.startswith(f"{name}."))
    return targets


def _violations_in_source(rel: str, source: str) -> dict[int, str]:
    """Line number -> the forbidden root that line reaches, in line order.

    Keyed by line so two imports of the same root on one line count once, and
    carrying the root so the passing message can report both halves of the
    migration separately instead of guessing from the line's text.
    """
    tree = ast.parse(source)
    hits: dict[int, str] = {}
    importers = _dynamic_importer_names(tree)
    for node in ast.walk(tree):
        # These are the only nodes that can name a module, and narrowing to them
        # here is also what lets `node.lineno` below be read off a known type
        # instead of the bare `ast.AST` that `ast.walk` yields.
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
            continue
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = (
                _absolute_from_relative(rel, node.level, node.module)
                if node.level
                else (node.module or "")
            )
            # The from-target alone is not enough. `from kiro_crew import acp`
            # has `node.module == "kiro_crew"`, which matches no forbidden root,
            # while the NAME it binds is the forbidden package itself -- and with
            # no inline opt-out marker this ordinary spelling would have been the
            # standard way around the gate. So each bound name is also resolved
            # against the from-target. `*` is skipped: a star-import of an
            # ancestor does not name a forbidden root.
            targets = [base] + [
                f"{base}.{alias.name}" if base else alias.name
                for alias in node.names
                if alias.name != "*"
            ]
        elif isinstance(node, ast.Call):
            callee = node.func
            name = (
                callee.attr
                if isinstance(callee, ast.Attribute)
                else callee.id if isinstance(callee, ast.Name) else ""
            )
            if name in importers:
                targets = _dynamic_import_targets(rel, node)
        for dotted in targets:
            root = _matched_root(dotted)
            if root is not None:
                hits.setdefault(node.lineno, root)
    return dict(sorted(hits.items()))


def _scan(targets: tuple[str, ...]) -> dict[str, dict[int, str]]:
    found: dict[str, dict[int, str]] = {}
    for target in targets:
        base = ROOT / target
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if _is_exempt(rel):
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # pragma: no cover - unreadable file
                raise SystemExit(f"cannot read {rel}: {exc}") from exc
            try:
                lines = _violations_in_source(rel, source)
            except SyntaxError as exc:
                raise SystemExit(
                    f"{rel} does not parse ({exc}); refusing to report it as clean, "
                    "because a shrink-only ratchet would then prune its real entry"
                ) from exc
            if lines:
                found[rel] = lines
    return found


def _read_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise SystemExit(
            f"baseline {path} is missing; restore it from git rather than "
            "regenerating it, since a regenerated baseline would silently absorb "
            "every edge added since it was recorded"
        )
    entries: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        count_str, _, rel = line.partition(" ")
        if not count_str.isdigit() or not rel:
            raise SystemExit(f"malformed baseline line: {line!r}")
        if rel in entries:
            raise SystemExit(
                f"duplicate baseline entry for {rel}; a later duplicate would "
                "silently override the recorded ceiling"
            )
        entries[rel] = int(count_str)
    return entries


def _write_baseline(path: Path, entries: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{count} {rel}\n" for rel, count in sorted(entries.items()))
    path.write_text(HEADER + body, encoding="utf-8")


def _display(path: Path) -> str:
    """Render a path for humans: repo-relative when it is inside the checkout.

    `--baseline` accepts an arbitrary path, so `relative_to(ROOT)` is not total.
    It used to be called in the seed success line -- AFTER the file had already
    been written -- so seeding to a path outside the checkout wrote the baseline
    and then died with a bare ValueError and a non-zero exit, which reads as
    "seeding failed" for a seed that in fact succeeded.
    """
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _shrunken_baseline(baseline: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    """The refresh result: counts only ever lowered, clean/gone entries dropped."""
    survivors: dict[str, int] = {}
    for rel, recorded in baseline.items():
        now = current.get(rel, 0)
        if now > 0:
            survivors[rel] = min(recorded, now)
    return survivors


def _verdicts(
    violations: dict[str, dict[int, str]],
    baseline: dict[str, int],
    changed: set[str] | None,
    added: dict[str, set[int]] | None,
) -> tuple[list[str], list[str], dict[str, list[int]], list[str]]:
    """Split the scan against the baseline into the four things worth printing.

    Every verdict is gated on `in_scope`, so a PR is only ever failed for a file
    it actually touched. Without that the gate is not a per-PR check but a shared
    tripwire: an edge landing anywhere in the tree turns the next unrelated PR
    red, and the author has no fix available in their own diff. Whoever grew the
    file is the PR that gets the error, because the file is in THEIR scope.
    """
    current = {rel: len(lines) for rel, lines in violations.items()}
    in_scope = (lambda rel: True) if changed is None else (lambda rel: rel in changed)

    new_offenders = sorted(rel for rel in current if rel not in baseline and in_scope(rel))
    grown = sorted(
        rel for rel in current if rel in baseline and current[rel] > baseline[rel] and in_scope(rel)
    )
    shrunk = sorted(
        rel for rel in baseline if current.get(rel, 0) < baseline[rel] and in_scope(rel)
    )

    added_line_offenders: dict[str, list[int]] = {}
    if added is not None:
        for rel, lines in violations.items():
            if rel in new_offenders or rel in grown or rel not in baseline:
                continue
            touched = sorted(set(lines) & added.get(rel, set()))
            if touched:
                added_line_offenders[rel] = touched

    return new_offenders, grown, added_line_offenders, shrunk


def _split_counts(violations: dict[str, dict[int, str]]) -> dict[str, int]:
    """Edge count per forbidden root, so both halves of the migration are visible."""
    counts = {root: 0 for root in FORBIDDEN_ROOTS}
    for roots in violations.values():
        for root in roots.values():
            counts[root] = counts.get(root, 0) + 1
    return counts


def seed_baseline(baseline_path: Path) -> int:
    """Write the first baseline. Refuses to overwrite an existing one.

    `--update-baseline` can only lower and prune, so it cannot produce the
    initial file, and `_read_baseline` deliberately fails closed when the file is
    absent. This flag closes that one gap without opening a laundering hole: if
    the baseline already exists, re-seeding is exactly the "regenerate and absorb
    every new offender" move the missing-file error exists to prevent, so it is
    refused and the caller is sent to `--update-baseline`.
    """
    if baseline_path.exists():
        raise SystemExit(
            f"{baseline_path} already exists; seeding again would absorb every "
            "edge added since it was recorded. Use --update-baseline, which only "
            "lowers counts and deletes lines."
        )
    violations = _scan(DEFAULT_TARGETS)
    entries = {rel: len(lines) for rel, lines in violations.items()}
    _write_baseline(baseline_path, entries)
    by_root = _split_counts(violations)
    print(
        f"seeded {_display(baseline_path)}: {sum(entries.values())} edge(s) "
        f"in {len(entries)} file(s) ("
        + ", ".join(f"{n} via {r}" for r, n in by_root.items())
        + ")."
    )
    return 0


def run_gate(baseline_path: Path, update: bool) -> int:
    violations = _scan(DEFAULT_TARGETS)
    current = {rel: len(lines) for rel, lines in violations.items()}
    baseline = _read_baseline(baseline_path)

    if update:
        survivors = _shrunken_baseline(baseline, current)
        pruned = len(baseline) - len(survivors)
        lowered = sum(1 for rel in survivors if survivors[rel] < baseline[rel])
        _write_baseline(baseline_path, survivors)
        print(f"pruned {pruned} entr(y/ies), lowered {lowered}; {len(survivors)} remain")
        return 0

    scope = _load_scope()
    changed, scope_label = scope.changed_paths()
    print(f"agent-sdk-boundary gate scope: {scope_label}", end="")
    print("" if changed is None else f" ({len(changed)} changed file(s))")
    added = scope.added_lines(scope_label) if changed is not None else None

    new_offenders, grown, added_line_offenders, shrunk = _verdicts(
        violations, baseline, changed, added
    )

    for rel in new_offenders:
        print(
            f"::error file={rel}::imports the ACP layer. Application code must "
            "reach the agent backend through kiro_crew.agent_sdk."
        )
        for line in violations[rel]:
            print(f"  {rel}:{line}")
    for rel in grown:
        print(
            f"::error file={rel}::ACP-layer imports grew from {baseline[rel]} to "
            f"{current[rel]}. The baseline is shrink-only."
        )
        for line in violations[rel]:
            print(f"  {rel}:{line}")
    for rel, lines in added_line_offenders.items():
        print(
            f"::error file={rel}::this change ADDS an ACP-layer import (the "
            "baseline covers only pre-existing lines)."
        )
        for line in lines:
            print(f"  {rel}:{line}")
    if shrunk:
        print(
            f"::error::{len(shrunk)} baselined file(s) now have fewer ACP-layer "
            "imports. Record the progress so the baseline keeps shrinking: "
            "python3 scripts/check_agent_sdk_boundary.py --update-baseline"
        )
        for rel in shrunk:
            print(f"  {rel}: {baseline[rel]} -> {current.get(rel, 0)}")

    if new_offenders or grown or added_line_offenders or shrunk:
        print(
            f"\nagent-sdk-boundary gate FAILED: {len(new_offenders)} new "
            f"offender(s), {len(grown)} grown count(s), "
            f"{len(added_line_offenders)} file(s) with new edges on added lines, "
            f"{len(shrunk)} entr(y/ies) to prune."
        )
        return 1

    by_root = _split_counts(violations)
    breakdown = ", ".join(f"{n} via {r}" for r, n in by_root.items())
    print(
        "agent-sdk-boundary gate passed: nothing in scope imports the ACP layer "
        f"outside the baseline ({sum(baseline.values())} known edge(s) in "
        f"{len(baseline)} file(s) still listed; {breakdown})."
    )
    return 0


def _self_test() -> int:
    """Plant one probe per rule family; a broken rule fails here, not in prod."""
    rel = "src/kiro_crew/dashboard/probe.py"
    flagged = {
        "plain from-import": "from kiro_crew.acp.types import AcpEvent\n",
        "module import": "import kiro_crew.acp.client\n",
        "providers re-export": "from kiro_crew.providers.base import LLMEvent\n",
        "package root": "import kiro_crew.providers\n",
        "multi-line": (
            "from kiro_crew.acp.types import (\n    AcpEvent,\n    EVENT_COMPLETE,\n)\n"
        ),
        "TYPE_CHECKING only": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.acp.runtime import AcpRuntime\n"
        ),
        "relative import": "from ..acp.types import AcpEvent\n",
        "dynamic import_module": (
            "import importlib\n" "importlib.import_module('kiro_crew.acp.client')\n"
        ),
        "dunder import": "__import__('kiro_crew.providers.acp')\n",
        "dynamic keyword name=": (
            "import importlib\n" "importlib.import_module(name='kiro_crew.acp.client')\n"
        ),
        "dynamic aliased importer": (
            "from importlib import import_module as _im\n" "_im('kiro_crew.acp.client')\n"
        ),
        "dynamic importer bound by assignment": (
            "import importlib\n"
            "_im = importlib.import_module\n"
            "_im('kiro_crew.providers.acp')\n"
        ),
        "dunder fromlist names the package": ("__import__('kiro_crew', fromlist=('acp',))\n"),
        "dunder fromlist list form": ("__import__('kiro_crew', fromlist=['providers'])\n"),
        "dunder fromlist dict keys": ("__import__('kiro_crew', fromlist={'acp': True})\n"),
        "fromlist starred unpack": ("x = ['acp']\n__import__('kiro_crew', fromlist=(*x,))\n"),
        "fromlist dict unpack": ("__import__('kiro_crew', fromlist={**{'acp': True}})\n"),
        "fromlist literal concat": ("__import__('kiro_crew', fromlist=('acp',) + ())\n"),
        "fromlist opaque call": ("__import__('kiro_crew', fromlist=frozenset({'acp'}))\n"),
        "fromlist comprehension": ("__import__('kiro_crew', fromlist=[m for m in ('acp',)])\n"),
        "fromlist name reference": ("f = ['acp']\n__import__('kiro_crew', fromlist=f)\n"),
        "dunder fromlist set": "__import__('kiro_crew', fromlist={'providers'})\n",
        "dynamic relative via positional package": (
            "import importlib\n" "importlib.import_module('.acp', 'kiro_crew')\n"
        ),
        "dynamic relative via two-dot positional package": (
            "import importlib\n" "importlib.import_module('..acp', 'kiro_crew.dashboard')\n"
        ),
        "dynamic relative via package=": (
            "import importlib\n" "importlib.import_module('.acp', package='kiro_crew')\n"
        ),
        "dunder relative via level=": ("__import__('acp', level=2)\n"),
        "empty name, purely relative": ("__import__('', globals(), locals(), ('acp',), 2)\n"),
        "empty name, relative by keyword": ("__import__('', fromlist=('providers',), level=2)\n"),
        "bound name is the package (acp)": "from kiro_crew import acp\n",
        "bound name is the package (providers)": "from kiro_crew import providers\n",
        "bound name via relative ancestor": "from .. import acp\n",
        "bound name aliased": "from kiro_crew import acp as _a\n",
    }
    clean = {
        "sdk import": "from kiro_crew.agent_sdk import AgentEvent\n",
        "unrelated kiro_crew": "from kiro_crew.session import SessionManager\n",
        "name collision": "from kiro_crew.acpx import thing\n",
        "bound name is an unrelated sibling": "from kiro_crew import session\n",
        "star-import of an ancestor": "from kiro_crew import *\n",
        "string mention only": "MSG = 'see kiro_crew.acp.types for the event'\n",
        "dynamic fromlist on an unrelated package": (
            "__import__('kiro_crew', fromlist=('session',))\n"
        ),
        "dynamic star fromlist names no root": ("__import__('kiro_crew', fromlist=('*',))\n"),
        "opaque fromlist under a sibling package": (
            "f = compute()\n__import__('kiro_crew.sandbox', fromlist=f)\n"
        ),
        "empty fromlist": "__import__('kiro_crew', fromlist=())\n",
        "empty name with no level": "__import__('')\n",
        "import_module with an empty name raises at runtime": (
            "import importlib\nimportlib.import_module('', package='kiro_crew.acp')\n"
        ),
        "dynamic non-literal": (
            "import importlib\n" "mod = 'kiro_crew.acp'\n" "importlib.import_module(mod)\n"
        ),
    }

    failures: list[str] = []
    for label, source in flagged.items():
        if not _violations_in_source(rel, source):
            failures.append(f"probe NOT flagged (rule is dead): {label}")
    for label, source in clean.items():
        if _violations_in_source(rel, source):
            failures.append(f"clean probe WAS flagged (rule is too wide): {label}")

    if not _is_exempt("src/kiro_crew/agent_sdk/drivers/acp.py"):
        failures.append("the SDK driver tree is not exempt")
    if _is_exempt("src/kiro_crew/dashboard/chat_runner.py"):
        failures.append("a consumer tree is exempt")

    for line in failures:
        print(f"::error::{line}")
    if failures:
        print(f"\nself-test FAILED: {len(failures)} broken rule(s).")
        return 1
    print(
        f"self-test passed: {len(flagged)} violation probe(s) flagged, "
        f"{len(clean)} clean probe(s) ignored."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="prune and lower recorded counts; never adds or raises one",
    )
    parser.add_argument(
        "--seed-baseline",
        action="store_true",
        help="write the FIRST baseline; refuses if one already exists",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="run the rule self-test instead of the gate",
    )
    args = parser.parse_args(argv)
    if args.test:
        return _self_test()
    if args.seed_baseline:
        return seed_baseline(args.baseline)
    return run_gate(args.baseline, args.update_baseline)


if __name__ == "__main__":
    sys.exit(main())
