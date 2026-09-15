"""Guard: code that WRITES into the data home must target the current one.

``test_skill_home_paths.py`` covers skill *guidance*. This module covers the
other half of the same bug class -- runtime **write** paths -- and exists because
five of them were missed by the ``~/.kirocrew`` -> ``~/.kiro/crew`` data-home
move and kept re-creating the abandoned home seconds after every launch:

* ``config/defaults.json`` -- the default ``postToolUse`` bash-audit hook.
  ``build_agent_config()`` overwrites the generated spec's hooks from the bundled
  defaults on every launch (so a user override cannot drop the ``PreToolUse``
  security gate), so this literal is the ONLY place the path can be corrected --
  hand-editing the generated spec regresses on the next start.
* ``mcp_gateway/stub_wrapper.sh`` -- the invocation log's fallback. kiro-cli
  strips env when spawning MCP subprocesses, so the fallback is the branch that
  actually runs.
* ``scripts/install-demo-app.sh`` -- installed the demo app into the legacy home.
* ``scripts/refresh-playwright-cookies.py`` -- wrote the browser storage state
  there with ``O_CREAT``, so every refresh resurrected the directory.
* ``packages/kirocrew-client-py`` -- the shipped client defaulted to the legacy
  home, so ``_read_app_secret`` silently returned ``""`` (app auth degraded to
  unauthenticated) and ``get_app_data_dir`` handed callers a legacy-rooted dir.

Between them the legacy dir was re-created on every boot, and the data-home
conflict WARNING in ``config/paths.py`` reported it as debris forever --
un-actionable, because deleting it just brought it back.

Scope, stated honestly: these tests assert on **write targets in code**, across
four shapes -- shell ``${KIROCREW_HOME:-...}`` fallbacks, bare legacy literals on
non-comment shell lines, Python ``KIROCREW_HOME`` defaults in shipped packages,
and Python ``expanduser`` of a hardcoded legacy literal. Comment/doc prose is
deliberately NOT asserted (a comment cannot create a directory;
``test_skill_home_paths.py`` owns guidance text), and neither are modules that
reference the legacy home *on purpose* to seed from it or keep
detecting it (``config/paths.py``, ``security.py``,
``sandbox.py``, ``history.py``, ``cloud/source.py``,
``apps/builtins/file_explorer/server.py``, ``kiro_prerequisite.py``,
``instances/token_mint.py``, plus the two allow-listed scripts below).
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS_JSON = REPO_ROOT / "src" / "kiro_crew" / "config" / "defaults.json"

# ``.kirocrew`` NOT followed by ``-`` or ``.`` -- so the top-level siblings that
# genuinely did not move (``.kirocrew-pods``, ``.kirocrew-dev``,
# ``.kirocrew.breadcrumb``, ``.kirocrew.archived``) are not false positives.
LEGACY_HOME = re.compile(r"\.kirocrew(?![-.])")

CURRENT_HOME = "/.kiro/crew"

# Scripts whose bare legacy reference is a deliberate READ, not a write target,
# and must therefore stay. Keyed on repo-RELATIVE paths, not basenames: the scan
# is repo-wide, so a basename key would hand an unconditional pass to any future
# file that happened to share the name.
LEGACY_READER_SCRIPTS = frozenset(
    {
        # seeds a dev home FROM the pre-move home on a not-yet-migrated box
        "dev-seed.sh",
        # sensitive-path denylist; must still refuse the legacy tree
        "src/kiro_crew/deploy/skills/artifact-deploy/scripts/_common.sh",
    }
)

# Directories that are vendored, generated, or dependency trees.
SKIP_DIR_PARTS = frozenset({"node_modules", "_vendor", ".venv", "build", "dist", ".git"})


@functools.lru_cache(maxsize=1)
def _repo_python_files() -> tuple[Path, ...]:
    """Every repo ``*.py`` outside :data:`SKIP_DIR_PARTS`, pruned DURING the walk.

    ``REPO_ROOT.rglob("*.py")`` enumerates the skipped trees before the caller can
    filter them out -- measured 4946 files walked in ~4s to keep 665, on a checkout
    with a ``.venv``. ``os.walk`` lets the skip list prune ``dirnames`` in place, so
    those subtrees are never descended into. Same file set, same assertions.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_PARTS]
        for name in filenames:
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
    return tuple(sorted(found))


def _shell_scripts() -> list[Path]:
    """Every TRACKED shell script in the repo, vendored/generated trees excluded.

    Asks git for the file list rather than walking the filesystem: a bare
    ``rglob`` also descends GITIGNORED directories, so a developer's local data
    home (``.kirocrew-dev/``, which legitimately contains installed skill
    scripts naming the legacy path) or any scratch checkout would fail this gate
    on their machine while CI stayed green. Only committed files can actually
    ship, so only committed files are in scope.

    Falls back to the filesystem walk when git is unavailable (e.g. an sdist
    with no ``.git``), preserving the original behavior there.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.sh"],
            capture_output=True,
            check=True,
            timeout=30,
        )
        paths = [REPO_ROOT / n for n in out.stdout.decode().split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        paths = sorted(REPO_ROOT.rglob("*.sh"))
    return [
        p
        for p in sorted(paths)
        if p.is_file() and not SKIP_DIR_PARTS.intersection(p.relative_to(REPO_ROOT).parts)
    ]


def _shipped_python() -> list[Path]:
    """Python that ships to users: the package plus the standalone client packages.

    ``test/`` is excluded on purpose -- test fixtures legitimately construct the
    legacy path to assert migration and sensitive-path behavior.
    """
    roots = [REPO_ROOT / "src" / "kiro_crew", REPO_ROOT / "packages"]
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        files += [
            p
            for p in sorted(root.rglob("*.py"))
            if not SKIP_DIR_PARTS.intersection(p.relative_to(REPO_ROOT).parts)
        ]
    return files


def test_default_audit_hook_writes_to_the_current_data_home() -> None:
    """The packaged ``postToolUse`` hook must not append into the legacy home."""
    hooks = json.loads(DEFAULTS_JSON.read_text(encoding="utf-8"))["hooks"]
    audit = [c["command"] for c in hooks["postToolUse"] if "audit.log" in c["command"]]
    assert audit, "expected a default audit hook in defaults.json"
    for command in audit:
        assert not LEGACY_HOME.search(command), (
            "the default audit hook writes into the pre-move legacy home; it is "
            "baked into every generated agent spec, so this regresses on each "
            f"launch: {command}"
        )
        assert (
            CURRENT_HOME in command
        ), f"the audit hook should target {CURRENT_HOME}, got: {command}"


def test_no_shell_home_default_points_at_the_legacy_home() -> None:
    """Every ``${KIROCREW_HOME:-...}`` fallback must name the current home."""
    scripts = _shell_scripts()
    assert len(scripts) > 5, f"guard scanned too few scripts: {len(scripts)}"

    pattern = re.compile(r"\$\{KIROCREW_HOME:-([^}]*)\}")
    offenders: list[str] = []
    for path in scripts:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for default in pattern.findall(line):
                if LEGACY_HOME.search(default):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "A shell KIROCREW_HOME fallback still names the pre-move home. Unset is "
        "the COMMON case (kiro-cli strips env for MCP subprocesses), so the "
        "fallback is what actually runs:\n  " + "\n  ".join(offenders)
    )


def test_no_shell_script_uses_a_bare_legacy_home_path() -> None:
    """No executable shell line may name the legacy home directly.

    Comment lines are skipped: prose cannot create a directory, and skill/doc
    wording is guarded by ``test_skill_home_paths.py``. Deliberate legacy
    *readers* are allow-listed above with their reason.
    """
    offenders: list[str] = []
    for path in _shell_scripts():
        # ``as_posix()`` (not ``str()``): on Windows ``relative_to`` renders
        # backslashes, which would miss the forward-slash allowlist entries and
        # redden only the Windows CI shard.
        if path.relative_to(REPO_ROOT).as_posix() in LEGACY_READER_SCRIPTS:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if LEGACY_HOME.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "A shell script names the pre-move legacy home on an executable line, so "
        "running it re-creates the abandoned directory. If the reference is a "
        "deliberate read (migration/seed/denylist), add the script to "
        "LEGACY_READER_SCRIPTS with a reason:\n  " + "\n  ".join(offenders)
    )


def test_no_shipped_python_defaults_to_the_legacy_home() -> None:
    """A shipped ``KIROCREW_HOME`` default must not resolve to the legacy home.

    Regression guard for the client package, where the legacy default made
    ``_read_app_secret`` return ``""`` -- silently downgrading app auth to
    unauthenticated -- rather than failing loudly.
    """
    # Only the DEFAULT is inspected, and only in real call sites -- docstring
    # prose mentioning the legacy home cannot match this shape.
    pattern = re.compile(
        r"""(?:os\.environ\.get|os\.getenv)\(\s*["']KIROCREW_HOME["']\s*,(?P<default>.*)$"""
    )
    offenders: list[str] = []
    for path in _shipped_python():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = pattern.search(line)
            if match and LEGACY_HOME.search(match.group("default")):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Shipped Python defaults KIROCREW_HOME to the pre-move home. Unset is the "
        "normal case, so the default is what runs:\n  " + "\n  ".join(offenders)
    )


def test_no_python_expands_a_hardcoded_legacy_home() -> None:
    """No Python may build a path by expanding a literal ``~/.kirocrew``.

    Fourth shape, and the one the other three could not see: a hardcoded
    ``os.path.expanduser("~/.kirocrew/...")`` is not a shell script, need not be
    in a shipped package, and never mentions ``KIROCREW_HOME`` -- which is how
    ``scripts/refresh-playwright-cookies.py`` kept re-creating the legacy home.

    Scanned repo-wide, excluding ``test/`` where fixtures legitimately construct
    the legacy path to exercise migration behavior.
    """
    pattern = re.compile(r"expanduser\(\s*[^)]*\.kirocrew(?![-.])")
    offenders: list[str] = []
    for path in _repo_python_files():
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] == "test":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert (
        not offenders
    ), "Python expands a hardcoded legacy home into a real path:\n  " + "\n  ".join(offenders)
