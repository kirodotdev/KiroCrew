#!/usr/bin/env python3
"""Enumerate behavior-preserving simplification candidates in the Kiro Crew backend.

Two candidate classes, both mechanically decidable:

  shadow-import   an in-function ``import X`` whose every binding is ALREADY bound
                  at module scope in the same file. The inner statement re-binds
                  the same ``sys.modules`` singleton, so deleting it is a no-op.

  chained-ternary an ``IfExp`` whose ``body`` or ``orelse`` is itself an ``IfExp``
                  (``a if c1 else b if c2 else d``), where the reader has to unpack
                  precedence to learn which branch wins.

Deliberately NOT reported, because acting on them is unsafe or out of scope:

  * ``from X import Y`` shadows. Such an import resolves ``X.Y`` at CALL time, so a
    test substituting the attribute on the source module observes it while the
    module-level binding (captured at import time) does not. Deleting one silently
    breaks that test's mock, and several such symbols are security controls whose
    tests would keep passing while verifying nothing.
  * a name bound at module scope ONLY under ``if TYPE_CHECKING:`` — unbound at
    runtime, so the in-function import is load-bearing and deleting it is a
    NameError.
  * an import inside ``try: ... except ImportError:`` (optional dependency).
  * an import carrying a ``# circular import`` marker, including one written INSIDE
    a parenthesized form on the imported name's own line.
  * a ternary that merely CONTAINS a ternary as a sub-expression of its result
    (``(a if c else b).method() if x else []``) — it reads left-to-right.
  * a chained ternary in a keystone security file. Reshaping one keystone audit
    label but not its twin is real, measured drift, so these are printed as
    EXCLUDED and never counted as work.

The boot-path set is READ FROM the scanned checkout's ``AUTOSDE.yaml`` rather than
copied here, so a change to that rule's ``file-patterns`` cannot leave this script
silently stale. Stdlib only, so it runs under any ``python3``.

Run from the Kiro Crew worktree root:
    scan_module.py                     # whole backend, per-module queue
    scan_module.py --module dashboard  # one module, site-level detail
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import sys
from collections import defaultdict
from pathlib import Path

# The AUTOSDE rule whose file set marks "an eager import here is a blocking
# violation". Its patterns are read from the checkout, never restated here.
BOOT_PATH_RULE_ID = "no-new-work-on-gateway-boot-path"

# Keystone security files: a matcher or audit-label SHAPE here is not worth
# churning for style. There is no machine-readable source for this set, so it is
# curated -- and validated against the checkout on every run (see
# _keystone_paths) so a rename or deletion fails loudly instead of quietly
# un-protecting a file.
KEYSTONE_RELATIVE = (
    "src/kiro_crew/security.py",
    "src/kiro_crew/hooks.py",
    "src/kiro_crew/sel.py",
    "src/kiro_crew/safety_override.py",
    "src/kiro_crew/sandbox.py",
    "src/kiro_crew/validation.py",
    "src/kiro_crew/platform/governance.py",
)

CIRCULAR = re.compile(r"circular", re.I)


def _boot_path_patterns(root: Path) -> list[str]:
    """The boot-path rule's `file-patterns`, read from the checkout's AUTOSDE.yaml.

    A tiny targeted reader rather than a YAML parse, to keep this script stdlib-only
    so it runs under any `python3`. The shape it depends on is the rule list's:

        - id: <rule-id>
          file-patterns:
            - "src/..."
          rule: |

    Exits nonzero if the rule or its patterns are absent, because falling back to a
    hardcoded copy is exactly the silent staleness this function exists to remove.
    """
    autosde = root / "AUTOSDE.yaml"
    if not autosde.is_file():
        sys.exit(f"cannot read the boot-path rule: {autosde} is missing")
    patterns: list[str] = []
    in_rule = False
    in_patterns = False
    for raw in autosde.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("- id:"):
            # A new rule entry ends the previous one.
            in_rule = stripped.split(":", 1)[1].strip() == BOOT_PATH_RULE_ID
            in_patterns = False
            continue
        if not in_rule:
            continue
        if stripped.startswith("file-patterns:"):
            in_patterns = True
            continue
        if in_patterns:
            if stripped.startswith("- "):
                patterns.append(stripped[2:].strip().strip("\"'"))
                continue
            # Any other key at rule level ends the pattern list.
            if stripped and not stripped.startswith("#"):
                in_patterns = False
    if not patterns:
        sys.exit(
            f"cannot read the boot-path rule: no `file-patterns` for "
            f"{BOOT_PATH_RULE_ID!r} in {autosde}. The rule may have been renamed — "
            "update BOOT_PATH_RULE_ID rather than hardcoding a file list."
        )
    return patterns


def _keystone_paths(root: Path) -> list[str]:
    """Curated keystone set, validated against the checkout.

    A path that no longer exists means the set is stale and some file is now
    unprotected, which is the failure this validation makes loud.
    """
    missing = [rel for rel in KEYSTONE_RELATIVE if not (root / rel).is_file()]
    if missing:
        sys.exit(
            "keystone list is stale — these paths are not in the checkout: "
            f"{missing}. Update KEYSTONE_RELATIVE; a renamed keystone file would "
            "otherwise be silently reported as ordinary work."
        )
    return list(KEYSTONE_RELATIVE)


def _is_test(path: Path) -> bool:
    parts = path.parts
    return (
        "tests" in parts
        or "_vendor" in parts
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


def _bindings(node: ast.stmt) -> list[tuple[str, str]]:
    """Normalized (source, bound_name) pairs for one import statement."""
    out: list[tuple[str, str]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            out.append((alias.name, alias.asname or alias.name.split(".")[0]))
    elif isinstance(node, ast.ImportFrom):
        src = ("." * node.level) + (node.module or "")
        for alias in node.names:
            out.append((f"{src}.{alias.name}", alias.asname or alias.name))
    return out


class _ModuleScope(ast.NodeVisitor):
    """Names an import UNCONDITIONALLY binds at module scope, and nothing else.

    Does not descend into ``def``/``class`` bodies: a name bound only inside some
    other function is not bound at module scope.

    Only an import at the top level of the module counts. An import nested under ANY
    ``if`` or ``try`` does NOT, because the name may be unbound at runtime:

        if TYPE_CHECKING:            # never bound at runtime
            from x import Y
        if IS_POSIX:                 # unbound on Windows
            import fcntl
        try:                         # unbound, or bound to a stub, without the dep
            import numpy as np
        except ImportError:
            np = None

    Treating any of those as guaranteed would make an in-function re-import look
    deletable, and deleting it is a NameError on the platform or install where the
    guard is false. This repo ships macOS, Linux and Windows, so that is reachable.
    The whole detector errs toward not reporting; this is where that starts.
    """

    def __init__(self) -> None:
        self.runtime: set[tuple[str, str]] = set()
        self._conditional = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_If(self, node: ast.If) -> None:
        self._conditional += 1
        self.generic_visit(node)
        self._conditional -= 1

    def visit_Try(self, node: ast.Try) -> None:
        self._conditional += 1
        self.generic_visit(node)
        self._conditional -= 1

    def _record(self, node: ast.stmt) -> None:
        if self._conditional:
            return
        self.runtime.update(_bindings(node))

    def visit_Import(self, node: ast.Import) -> None:
        self._record(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._record(node)


class _Sites(ast.NodeVisitor):
    def __init__(self, runtime: set[tuple[str, str]], source: str) -> None:
        self.runtime = runtime
        self.lines = source.splitlines()
        self._fn = 0
        self._guard: list[str] = []
        self.shadow_plain: list[dict] = []
        self.ternaries: list[dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._fn += 1
        self.generic_visit(node)
        self._fn -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Try(self, node: ast.Try) -> None:
        optional_dep = any(
            (isinstance(h.type, ast.Name) and h.type.id in {"ImportError", "ModuleNotFoundError"})
            or (
                isinstance(h.type, ast.Tuple)
                and any(
                    isinstance(e, ast.Name) and e.id in {"ImportError", "ModuleNotFoundError"}
                    for e in h.type.elts
                )
            )
            or h.type is None
            for h in node.handlers
        )
        self._guard.append("optional-dep" if optional_dep else "try")
        self.generic_visit(node)
        self._guard.pop()

    def visit_IfExp(self, node: ast.IfExp) -> None:
        if isinstance(node.body, ast.IfExp) or isinstance(node.orelse, ast.IfExp):
            self.ternaries.append(
                {"line": node.lineno, "text": self.lines[node.lineno - 1].strip()[:160]}
            )
        self.generic_visit(node)

    def _marked_circular(self, node: ast.stmt) -> bool:
        """A `# circular import` marker above the statement or inside its body.

        The inside case matters: a parenthesized form often carries the marker on
        the imported name's own line, e.g.
            from kiro_crew.config.loader import (
                KiroCrewConfig,  # circular import: loader imports apps/ ...
            )
        """
        end = getattr(node, "end_lineno", node.lineno)
        lo = max(0, node.lineno - 3)
        return bool(CIRCULAR.search("\n".join(self.lines[lo:end])))

    def visit_Import(self, node: ast.Import) -> None:
        """Only a plain `import X` is ever reported (see the module docstring)."""
        if not self._fn or "optional-dep" in self._guard:
            return
        pairs = _bindings(node)
        if not pairs or self._marked_circular(node):
            return
        # Every binding must already exist at module scope AT RUNTIME. A partially
        # redundant statement still needs its remaining names.
        if not all(p in self.runtime for p in pairs):
            return
        end = getattr(node, "end_lineno", node.lineno)
        self.shadow_plain.append(
            {
                "line": node.lineno,
                "text": " ".join(x.strip() for x in self.lines[node.lineno - 1 : end])[:200],
            }
        )


# Label for files directly under src/kiro_crew/. Deliberately shell-safe and
# unquoted-friendly, because it is pasted straight into `--module <label>`; angle
# brackets would be read as a redirection.
TOPLEVEL_LABEL = "toplevel"


def _module_of(rel_to_pkg: Path) -> str:
    return rel_to_pkg.parts[0] if len(rel_to_pkg.parts) > 1 else TOPLEVEL_LABEL


def _matches_boot_path(rel: str, patterns: list[str]) -> bool:
    """True when `rel` is covered by any boot-path pattern.

    ``fnmatch`` gives ``/`` no special meaning, so ``a/**/*.py`` compiles to a regex
    that REQUIRES an intermediate directory and therefore misses ``a/core.py`` — the
    direct children the rule most obviously covers. Each pattern is also tried with
    ``/**/`` collapsed to ``/`` so zero-directory matches are caught. Under-matching
    here would silently drop the `boot-path` flag from a file the blocking rule
    protects, so this errs toward flagging.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if "/**/" in pattern and fnmatch.fnmatch(rel, pattern.replace("/**/", "/")):
            return True
    return False


def scan(root: Path, boot_patterns: list[str], keystone: list[str]) -> dict[str, dict]:
    pkg = root / "src" / "kiro_crew"
    if not pkg.is_dir():
        sys.exit(f"not a Kiro Crew checkout: {pkg} is missing")
    found: dict[str, dict] = {}
    pkg_resolved = pkg.resolve()
    for path in sorted(pkg.rglob("*.py")):
        if _is_test(path):
            continue
        # Read only regular files that really live inside the package. A symlink
        # pointing outside it (or at a character device) is not source this scanner
        # has any business reading, and an unbounded one would hang the scan.
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if not path.resolve().is_relative_to(pkg_resolved):
                continue
        except OSError:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        scope = _ModuleScope()
        scope.visit(tree)
        sites = _Sites(scope.runtime, source)
        sites.visit(tree)
        if not (sites.shadow_plain or sites.ternaries):
            continue
        rel = path.relative_to(root).as_posix()
        flags = []
        if _matches_boot_path(rel, boot_patterns):
            flags.append("boot-path")
        if rel in keystone:
            flags.append("keystone")
        found[rel] = {
            "module": _module_of(path.relative_to(pkg)),
            "flags": flags,
            "shadow_plain": sites.shadow_plain,
            "chained_ternaries": sites.ternaries,
        }
    return found


def _report_module(found: dict[str, dict], module: str) -> None:
    actionable = 0
    excluded = 0
    for rel, info in sorted(found.items()):
        keystone = "keystone" in info["flags"]
        flags = f"  [{','.join(info['flags'])}]" if info["flags"] else ""
        lines = [f"    shadow-import   :{s['line']:<5} {s['text']}" for s in info["shadow_plain"]]
        for site in info["chained_ternaries"]:
            if keystone:
                lines.append(
                    f"    ternary EXCLUDED (keystone) :{site['line']:<5} {site['text']}"
                )
            else:
                lines.append(f"    chained-ternary :{site['line']:<5} {site['text']}")
        actionable += len(info["shadow_plain"])
        if keystone:
            excluded += len(info["chained_ternaries"])
        else:
            actionable += len(info["chained_ternaries"])
        print(f"{rel}{flags}")
        print("\n".join(lines))
    print(f"\nactionable sites in {module}: {actionable}")
    if excluded:
        print(
            f"excluded as keystone (do NOT reshape): {excluded} — reshaping one "
            "keystone audit label but not its twin is the drift this exclusion prevents"
        )


def _report_queue(found: dict[str, dict]) -> None:
    tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for info in found.values():
        row = tally[info["module"]]
        row["files"] += 1
        row["shadow"] += len(info["shadow_plain"])
        # A keystone file's ternaries are excluded, so they must not be counted as
        # work -- otherwise the queue invites the inconsistency the rule prevents.
        if "keystone" in info["flags"]:
            row["excluded"] += len(info["chained_ternaries"])
        else:
            row["ternary"] += len(info["chained_ternaries"])
    header = f"{'module':22s} {'files':>6s} {'shadow':>7s} {'ternary':>8s} {'actionable':>11s}"
    print(header)
    print("-" * len(header))
    # Ascending, so the queue reads in the order the skill says to work it: small
    # modules first, dashboard/ and slack/ last.
    rows = sorted(tally.items(), key=lambda kv: (kv[1]["shadow"] + kv[1]["ternary"]))
    for module, row in rows:
        act = row["shadow"] + row["ternary"]
        print(f"{module:22s} {row['files']:6d} {row['shadow']:7d} {row['ternary']:8d} {act:11d}")
    print("-" * len(header))
    total = sum(r["shadow"] + r["ternary"] for r in tally.values())
    print(
        f"{'TOTAL':22s} {sum(r['files'] for r in tally.values()):6d} "
        f"{sum(r['shadow'] for r in tally.values()):7d} "
        f"{sum(r['ternary'] for r in tally.values()):8d} {total:11d}"
    )
    excluded = sum(r["excluded"] for r in tally.values())
    if excluded:
        print(f"(plus {excluded} keystone ternaries excluded, not counted as work)")
    print("\nOne module per branch, per commit, per PR. Order by `actionable`, ascending,")
    print("so small modules build reviewer trust before dashboard/ and slack/.")
    if total == 0:
        print("\nZero actionable sites does NOT mean zero work: these two classes are")
        print("finite and get consumed. What remains is the judgment classes in the skill.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--module", help="restrict to one module dir under src/kiro_crew/")
    args = parser.parse_args()

    # Run from the worktree root. `cd` into another checkout to scan it.
    root = Path.cwd().resolve()
    boot_patterns = _boot_path_patterns(root)
    keystone = _keystone_paths(root)

    found = scan(root, boot_patterns, keystone)
    if args.module:
        found = {k: v for k, v in found.items() if v["module"] == args.module}
        _report_module(found, args.module)
    else:
        _report_queue(found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
