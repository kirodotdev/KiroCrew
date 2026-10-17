"""The Linux Packaging lane triggers on every module its listed entry files load.

ci.yml's ``linux_packaging`` paths-filter bucket names the Electron entry files
that decide what a Linux package installs and which update feed it reads
(``auto-update.js``, ``bundle-location.js``). Those entries compose owner modules
through relative ``require`` calls, so a bucket keyed only on an entry path
skips the build-and-smoke-install job for a diff that edits an owner alone.

These tests walk the relative-require closure of each ``.js`` file the bucket
lists and assert that every file in it matches the bucket, so an owner added
under a path the bucket does not cover fails here instead of silently skipping
the lane.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_BUCKET = "linux_packaging"
_RELATIVE_REQUIRE = re.compile(r"""require\(\s*["'](\.{1,2}/[^"']+)["']\s*\)""")


def _bucket_patterns() -> list[str]:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    filter_step = next(
        step
        for step in workflow["jobs"]["changes"]["steps"]
        if "paths-filter" in str(step.get("uses", ""))
    )
    filters = yaml.safe_load(filter_step["with"]["filters"])
    patterns = filters[_BUCKET]
    assert patterns and all(isinstance(p, str) for p in patterns)
    # The matcher below understands positive globs only; a negation would
    # change what "covered" means.
    assert not [p for p in patterns if p.startswith("!")], patterns
    return patterns


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a paths-filter glob: ``**`` spans directories, ``*`` does not."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _covered(rel_path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(p).match(rel_path) for p in patterns)


def _resolve(base: Path, spec: str) -> Path | None:
    target = (base.parent / spec).resolve()
    for candidate in (target, target.with_name(target.name + ".js"), target / "index.js"):
        if candidate.is_file():
            return candidate
    return None


def _require_closure(entries: list[Path]) -> set[Path]:
    seen: set[Path] = set()
    pending = list(entries)
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        if path.suffix != ".js":
            continue
        for spec in _RELATIVE_REQUIRE.findall(path.read_text(encoding="utf-8")):
            resolved = _resolve(path, spec)
            assert resolved is not None, f"{path}: require({spec!r}) does not resolve"
            pending.append(resolved)
    return seen


def _listed_entries(patterns: list[str]) -> list[Path]:
    return [
        _REPO_ROOT / p
        for p in patterns
        if p.startswith("website/electron/") and p.endswith(".js") and "*" not in p
    ]


def test_the_glob_matcher_keeps_single_star_inside_one_directory() -> None:
    assert _covered(
        "website/electron/runtime/update/feed-lane.js", ["website/electron/runtime/update/**"]
    )
    assert _covered("website/electron/auto-update.js", ["website/electron/*.js"])
    assert not _covered("website/electron/runtime/update/feed-lane.js", ["website/electron/*.js"])
    assert not _covered("website/electron/auto-update.js.bak", ["website/electron/auto-update.js"])


def test_the_walk_reaches_the_owner_modules_the_entries_compose() -> None:
    entries = _listed_entries(_bucket_patterns())
    assert entries, f"the {_BUCKET} bucket lists no Electron .js entry file"
    closure = _require_closure(entries)
    assert len(closure) > len(entries), (
        "the require walk found no module beyond the listed entries -- "
        "_RELATIVE_REQUIRE no longer matches how the entries load their owners"
    )


def test_every_module_the_listed_entries_load_triggers_the_lane() -> None:
    patterns = _bucket_patterns()
    closure = _require_closure(_listed_entries(patterns))
    uncovered = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in closure
        if not _covered(path.relative_to(_REPO_ROOT).as_posix(), patterns)
    )
    assert not uncovered, (
        f"ci.yml's {_BUCKET} bucket skips the Linux Packaging job for a diff that "
        f"edits only these modules, which its listed entry files load: {uncovered}. "
        "Add their path (or their directory's '/**' glob) to the bucket."
    )
