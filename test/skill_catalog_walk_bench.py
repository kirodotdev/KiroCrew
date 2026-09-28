"""Time one skill-catalog walk against the snapshot's mtime re-check.

The agent PATCH's receipt (``agents.py``, ``_locked_overwrite``) is resolved off
the written spec against the catalog the mapping walked, and the skill roots are
walked a second time only when ``SkillCatalogSnapshot.changed()`` says they moved.
That re-check earns its place only if a second walk costs materially more than the
check itself. This script measures both on a tree of the shape the walk covers, so
the receipt path is shaped by numbers rather than by the claim that a second walk
"doubles the filesystem cost of every chip toggle".

Run from the repository root::

    PYTHONPATH=src python3 test/skill_catalog_walk_bench.py [--skills 200] [--runs 20]

It builds, in a temporary directory, a fake home (pinned as both ``HOME`` and
``USERPROFILE``, whichever ``Path.home()`` reads on this platform), a fake
``$KIROCREW_HOME`` and a project directory -- the three skill roots a vanilla
install keys (``kiro-user/``, ``kiro-workspace/`` and the data home, resolved
through the handler's own ``_skill_key_roots``) -- refuses to write anything if a
resolved root lies outside that directory, and spreads ``--skills`` skills over them,
half under the user root and a quarter under each of the others, one in five inside
a category directory, each with the ``SKILL.md`` the walk reads. With the tree at
rest (every directory backdated past the settle window) and the page cache warm, it
times, ``--runs`` times each:

* ``a`` -- one full walk, :func:`walk_skill_catalog` (what a second walk costs);
* ``b`` -- one :meth:`SkillCatalogSnapshot.changed` re-check over the snapshot of
  such a walk (what the re-check costs when nothing moved).

It prints the median and p95 of ``a``, ``b`` and their paired sum ``a+b`` (the
receipt path with the re-check, roots at rest) as a Markdown table, then ``2a`` (the
receipt path with an unconditional second walk). Wall clock, ``time.perf_counter``.
Nothing here is a test and nothing asserts on a duration.
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

_CATEGORIES = ("utils", "data", "ops")


class _Slot:
    def __init__(self, project: Path):
        self.project = str(project)
        self.total_messages = 1
        self.workspace = "default"


class _State:
    """Minimal DashboardState stand-in: one chat slot bound to *project*."""

    def __init__(self, project: Path):
        self._slots = {"chat-1": _Slot(project)}


def _make_skill(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\nBody of {name}\n",
        encoding="utf-8",
    )
    return d


def _build_tree(roots: list[Path], skills: int) -> None:
    """Spread *skills* skills over the three *roots*, 2:1:1, one in five nested."""
    weights = (0, 0, 1, 2)
    for i in range(skills):
        root = roots[weights[i % len(weights)]]
        parent = root / _CATEGORIES[i % len(_CATEGORIES)] if i % 5 == 0 else root
        _make_skill(parent, f"skill-{i:03d}")


def _settle(base: Path, age_s: float = 10.0) -> None:
    """Backdate every directory under *base* past the snapshot's settle window."""
    stamp = time.time() - age_s
    for d in [base, *(p for p in base.rglob("*") if p.is_dir())]:
        os.utime(d, (stamp, stamp))


def _time_ms(fn: Callable[[], object], runs: int) -> list[float]:
    out: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def _p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _row(label: str, values: list[float]) -> str:
    return f"| {label} | {statistics.median(values):.2f} | {_p95(values):.2f} |"


# The environment the run pins at its temporary tree. HOME is what ``Path.home()``
# reads on POSIX and USERPROFILE what it reads on Windows (never HOME there), so
# both are pinned or the ``kiro-user/`` root would land in the real profile.
_PINNED_ENV = ("HOME", "USERPROFILE", "KIROCREW_HOME")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skills", type=int, default=200, help="skills to spread over the roots")
    parser.add_argument("--runs", type=int, default=20, help="timed runs per measurement")
    args = parser.parse_args(argv)

    saved_env = {name: os.environ.get(name) for name in _PINNED_ENV}
    try:
        with tempfile.TemporaryDirectory(prefix="skill-catalog-walk-bench-") as tmp:
            return _measure(Path(tmp), args.skills, args.runs)
    finally:
        # Hand the process its own homes back: an in-process caller (the smoke
        # test) must not inherit a torn-down temporary tree as HOME.
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _measure(base: Path, skills: int, runs: int) -> int:
    """Build the tree under *base*, measure, print; ``2`` when the tree cannot be trusted."""
    home = base / "home"
    project = base / "project"
    home.mkdir()
    project.mkdir()
    # Set BEFORE the import: ``Path.home()`` reads the environment at call time,
    # and ``config_dir()`` memoises on the KIROCREW_HOME it first sees.
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ["KIROCREW_HOME"] = str(base / "kirocrew-home")

    import kiro_crew
    from kiro_crew.dashboard.handlers import _shared

    # A stand-in for the handler's DashboardState: the root resolver reads
    # only ``_slots``.
    state: Any = _State(project)
    session_key = "chat-1"
    keyed = _shared._skill_key_roots(state, session_key)
    # User, workspace and data home, in the resolver's precedence order; an
    # edition install adds ``package/`` roots after them, which stay empty.
    roots = [root for _prefix, root in keyed[:3]]
    if [prefix for prefix, _root in keyed[:3]] != ["kiro-user/", "kiro-workspace/", ""]:
        print(f"expected the three roots of a vanilla install, got {keyed!r}")
        return 2
    # Every root must sit inside the temporary tree BEFORE anything is written:
    # a root resolved from the real home would be built into (and left behind
    # in) the operator's own skills directory, and would not be at rest.
    base_resolved = base.resolve()
    escaped = [root for root in roots if not root.resolve().is_relative_to(base_resolved)]
    if escaped:
        names = ", ".join(str(root) for root in escaped)
        print(f"refusing to build outside the temporary tree {base}: {names}")
        return 2
    _build_tree(roots, skills)
    _settle(base)

    # Warm the page cache and the interpreter; prove the tree is what it claims.
    snapshot = _shared.walk_skill_catalog(state, session_key)
    if len(snapshot.entries) != skills:
        print(f"walk found {len(snapshot.entries)} skills, expected {skills}")
        return 2
    if snapshot.changed():
        print("the tree is not at rest: changed() is True before any change")
        return 2

    walk = _time_ms(lambda: _shared.walk_skill_catalog(state, session_key), runs)
    recheck = _time_ms(snapshot.changed, runs)
    if snapshot.changed():
        print("the tree moved during the measurement; rerun")
        return 2
    both = [a + b for a, b in zip(walk, recheck)]
    twice = [2 * a for a in walk]

    print(f"kiro_crew: {Path(kiro_crew.__file__).resolve().parents[2]}")
    print(f"python {platform.python_version()} on {platform.platform()}")
    print(
        f"tree: {len(snapshot.entries)} skills over {len(roots)} roots "
        f"({', '.join(prefix or 'data-home' for prefix, _root in keyed[:3])}), "
        f"{len(snapshot.dir_mtimes)} directories read by the walk and stat'ed by the re-check; "
        f"{runs} runs each, warm cache, roots at rest"
    )
    print()
    print("| Measured | median ms | p95 ms |")
    print("|---|---|---|")
    print(_row("a: one full walk (`walk_skill_catalog`)", walk))
    print(_row("b: `changed()` re-check, roots at rest", recheck))
    print(_row("a+b: receipt with the re-check (paired)", both))
    print()
    print(
        "2a: receipt with an unconditional second walk (paired): "
        f"median {statistics.median(twice):.2f} ms, p95 {_p95(twice):.2f} ms"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
