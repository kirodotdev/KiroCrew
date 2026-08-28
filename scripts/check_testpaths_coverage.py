#!/usr/bin/env python3
"""check_testpaths_coverage.py — every pytest test file must be collected.

``setup.cfg`` pins ``testpaths`` so pytest does not walk the vendored trees, and
that pin has a failure mode this gate exists to close: a test file created
OUTSIDE the collected roots is silently never run — not by CI and not by a bare
``pytest`` at the repo root — while looking exactly like a real test to every
reader. Issue #6577's headline: twelve files sat in a root-level ``tests/``
directory for months, green by omission, and rotted against the code they
claimed to cover (removed modules, renamed helpers, superseded contracts).

## What is flagged

Every tracked file whose name matches pytest's default ``python_files``
patterns (``test_*.py`` / ``*_test.py``) and whose path is not under one of the
``testpaths`` roots parsed from ``setup.cfg``.

## What is exempt, and why

* A file inside a NESTED DISTRIBUTION — some ancestor directory below the repo
  root carries its own ``pyproject.toml`` or ``setup.cfg`` (e.g.
  ``packages/kirocrew-client-py/``). That subtree has its own test runner and
  its own collection config; this repo's ``testpaths`` was never meant to reach
  it.
* A file whose first lines carry a ``testpaths-ok: <reason>`` marker — for the
  scripts that look like tests but are deliberately not pytest tests (e.g.
  ``docker/test_sandbox_integration.py``, a manually-invoked Docker probe run
  with ``python``, not ``pytest``). The reason is mandatory: a bare marker does
  not exempt.

Fails closed: a missing or empty ``testpaths`` in ``setup.cfg`` is itself an
error, because with no pin every heuristic below is meaningless.

## Usage

    python3 scripts/check_testpaths_coverage.py          # scan the repo
    python3 scripts/check_testpaths_coverage.py --test   # self-test the rules
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# pytest's default ``python_files`` patterns; setup.cfg does not override them.
_TEST_PREFIX = "test_"
_TEST_SUFFIX = "_test"

# How deep into a file the marker may sit (a shebang, encoding line, and a
# short comment block all fit; burying it below real code does not count).
_MARKER_WINDOW_LINES = 10
_MARKER = "testpaths-ok:"

# Files that declare a subtree to be its own distribution with its own runner.
_DIST_CONFIG_NAMES = frozenset({"pyproject.toml", "setup.cfg"})

REMEDY = (
    "Remedy: move the file under a collected testpaths root (see setup.cfg), or\n"
    "— if it is deliberately not a pytest test — add a comment in its first\n"
    f"{_MARKER_WINDOW_LINES} lines: '# {_MARKER} <why this is not collected>'."
)


def parse_testpaths(cfg_text: str) -> list[str]:
    """Return the testpaths roots from setup.cfg's [tool:pytest] section."""
    parser = configparser.ConfigParser()
    parser.read_string(cfg_text)
    raw = parser.get("tool:pytest", "testpaths", fallback="")
    return [p for p in raw.split() if p]


def looks_like_test_file(rel_path: str) -> bool:
    """Match pytest's default python_files patterns on the basename."""
    name = rel_path.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    stem = name[: -len(".py")]
    return stem.startswith(_TEST_PREFIX) or stem.endswith(_TEST_SUFFIX)


def is_under(rel_path: str, roots: list[str]) -> bool:
    """True when rel_path sits under (or is) one of the root directories."""
    return any(rel_path == root or rel_path.startswith(root + "/") for root in roots)


def in_nested_distribution(rel_path: str, dist_dirs: frozenset[str]) -> bool:
    """True when an ancestor dir BELOW the repo root has its own build config.

    ``dist_dirs`` holds the relative directories that contain a pyproject.toml
    or setup.cfg; the repo root itself ("") never exempts.
    """
    parts = rel_path.split("/")[:-1]
    ancestor = ""
    for part in parts:
        ancestor = f"{ancestor}/{part}" if ancestor else part
        if ancestor in dist_dirs:
            return True
    return False


def has_marker(text: str) -> bool:
    """True when a COMMENT line `# testpaths-ok: <reason>` sits in the first lines.

    Anchored to a comment token on purpose: prose in a module docstring that
    merely mentions the marker convention must not exempt the file — that would
    reintroduce the silent green-by-omission this gate exists to close.
    """
    for line in text.splitlines()[:_MARKER_WINDOW_LINES]:
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        idx = stripped.find(_MARKER)
        if idx == -1:
            continue
        reason = stripped[idx + len(_MARKER) :].strip()
        if reason:
            return True
    return False


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")
    return [p for p in out.split("\0") if p]


def find_violations(files: list[str], roots: list[str], read_text) -> tuple[list[str], list[str]]:
    """Classify every tracked path.

    Returns ``(violations, exempted)`` — the uncollected test files, and the
    test files excused by a nested distribution or a marker. The exempted list
    is printed on success so a silent scope collapse (e.g. a fixture tree
    gaining a ``setup.cfg`` and swallowing every test beneath it) shows up in
    the job log instead of reading as a clean tree.
    """
    dist_dirs = frozenset(
        p.rsplit("/", 1)[0]
        for p in files
        if "/" in p and p.rsplit("/", 1)[-1] in _DIST_CONFIG_NAMES
    )
    violations = []
    exempted = []
    for path in files:
        if not looks_like_test_file(path):
            continue
        if is_under(path, roots):
            continue
        if in_nested_distribution(path, dist_dirs):
            exempted.append(f"{path} (nested distribution)")
            continue
        text = read_text(path)
        if text is not None and has_marker(text):
            exempted.append(f"{path} ({_MARKER} marker)")
            continue
        violations.append(path)
    return violations, exempted


def _read_repo_text(rel_path: str) -> str | None:
    try:
        with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8", errors="replace") as fh:
            # Only the marker window is ever inspected.
            return "".join(fh.readline() for _ in range(_MARKER_WINDOW_LINES))
    except OSError:
        return None


# ── Self-test ──────────────────────────────────────────────────────────────

_CFG_WITH_ROOTS = "[tool:pytest]\ntestpaths = test src/kiro_crew/apps/builtins\n"

# (description, files, file_texts, expected violation count)
_PROBES = [
    (
        "file under the primary root is collected",
        ["test/test_x.py"],
        {},
        0,
    ),
    (
        "file under the builtins root is collected",
        ["src/kiro_crew/apps/builtins/foo/tests/test_x.py"],
        {},
        0,
    ),
    (
        "file in a stray root-level tests/ dir is flagged",
        ["tests/test_x.py"],
        {"tests/test_x.py": ""},
        1,
    ),
    (
        "suffix naming (_test.py) outside the roots is flagged",
        ["tools/smoke_test.py"],
        {"tools/smoke_test.py": ""},
        1,
    ),
    (
        "a prefix match on a directory name alone does not collect",
        ["testing/test_x.py"],  # "testing" is not the root "test"
        {"testing/test_x.py": ""},
        1,
    ),
    (
        "nested distribution with its own pyproject is exempt",
        ["packages/client/pyproject.toml", "packages/client/tests/test_client.py"],
        {},
        0,
    ),
    (
        "the repo root's own setup.cfg exempts nothing",
        ["setup.cfg", "tests/test_x.py"],
        {"tests/test_x.py": ""},
        1,
    ),
    (
        "marker with a reason exempts",
        ["docker/test_probe.py"],
        {
            "docker/test_probe.py": "#!/usr/bin/env python3\n# testpaths-ok: run manually via docker, not pytest\n"
        },
        0,
    ),
    (
        "bare marker without a reason does not exempt",
        ["docker/test_probe.py"],
        {"docker/test_probe.py": "# testpaths-ok:\n"},
        1,
    ),
    (
        "marker inside a docstring does not exempt",
        ["docker/test_probe.py"],
        {"docker/test_probe.py": '"""Documents the testpaths-ok: convention in prose."""\n'},
        1,
    ),
    (
        "marker below the window does not exempt",
        ["docker/test_probe.py"],
        {"docker/test_probe.py": ("#\n" * _MARKER_WINDOW_LINES) + "# testpaths-ok: too late\n"},
        1,
    ),
    (
        "non-test python files are ignored",
        ["tests/conftest.py", "tests/helpers.py", "tests/fixtures/attest_payload.py"],
        {},
        0,
    ),
]


def self_test() -> int:
    failures = 0
    roots = parse_testpaths(_CFG_WITH_ROOTS)
    assert roots == ["test", "src/kiro_crew/apps/builtins"], roots
    for desc, files, texts, expected in _PROBES:
        got, _exempted = find_violations(files, roots, lambda p: texts.get(p, ""))
        status = "ok" if len(got) == expected else "FAIL"
        if len(got) != expected:
            failures += 1
        print(f"  [{status}] {desc}: expected {expected}, got {len(got)}")
    # Fail-closed probe: an empty testpaths must be an error, not a pass.
    try:
        _resolve_roots("[tool:pytest]\naddopts = -q\n")
        print("  [FAIL] missing testpaths must fail closed")
        failures += 1
    except SystemExit:
        print("  [ok] missing testpaths fails closed")
    if failures:
        print(f"self-test: {failures} probe(s) failed", file=sys.stderr)
        return 1
    print(f"self-test: all {len(_PROBES) + 1} probes passed")
    return 0


def _resolve_roots(cfg_text: str) -> list[str]:
    roots = parse_testpaths(cfg_text)
    if not roots:
        print(
            "check_testpaths_coverage: setup.cfg has no [tool:pytest] testpaths — "
            "this gate needs the pin it exists to guard.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return roots


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return self_test()
    with open(os.path.join(REPO_ROOT, "setup.cfg"), encoding="utf-8") as fh:
        roots = _resolve_roots(fh.read())
    violations, exempted = find_violations(tracked_files(), roots, _read_repo_text)
    if violations:
        print(
            "check_testpaths_coverage: pytest test file(s) exist outside the\n"
            f"collected testpaths roots ({' '.join(roots)}). pytest NEVER runs\n"
            "them — not in CI and not locally — so they are green by omission\n"
            "and will rot against the code they claim to cover (see #6577).\n",
            file=sys.stderr,
        )
        for path in violations:
            print(f"  {path}", file=sys.stderr)
        print(f"\n{REMEDY}", file=sys.stderr)
        return 1
    print(
        f"check_testpaths_coverage: every pytest test file sits under a collected root ({' '.join(roots)})."
    )
    # Non-failing report, same reason every whole-tree gate prints its count:
    # a silent scope collapse (a fixture tree gaining a setup.cfg, a marker
    # spreading) must be visible in the job log, not identical to a clean tree.
    print(f"exempted: {len(exempted)} file(s)")
    for line in exempted:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
