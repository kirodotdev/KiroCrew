#!/usr/bin/env python3
"""black_cache_portable.py -- carry Black's own cache between CI checkouts.

## Why this exists

``main-ratchet-audit.yml`` runs the black gate over the whole tree on every push
to main, through ``scripts/bounded_black.py``, which spawns one worker process
per file to bound the compiled wheel's memory retention. Spawning and importing
Black once per file is most of the step: ~10,900 files took 13-22 minutes on the
CodeBuild runner, while native Black on the same tree takes a little over two.
Black already has a cache that skips files it has seen formatted, but every run
starts from an empty one.

Persisting Black's cache directory as-is does not work on the fleet. Black keys
each entry on the file's ABSOLUTE resolved path, and a CodeBuild runner checks
the repository out under a fresh ``/codebuild/output/src<random>/...`` root per
build, so a restored cache would match no file at all.

## What this does

``export`` reads, through Black's own ``black.cache.Cache.read``, the cache the
gate's run just wrote, and saves it as JSON: the cache file's name and, for each
file Black found formatted, its repository-relative path and the SHA-256 Black
recorded. ``restore`` hands Black, for the current checkout, every file whose
bytes still hash to the recorded value, and ``black.cache.Cache.write`` records
each one itself. Black reads and writes its own file format; this module only
ever parses JSON.

The cache file is the one for the gate's own ``black.Mode``: the pinned
``TARGET_VERSION`` from ``check_black_formatting.py`` (the CLI flag the gate
passes, which overrides ``[tool.black] target-version``) and ``[tool.black]
line-length``, read with Black's own ``parse_pyproject_toml``. Any other
``[tool.black]`` key that could change the mode turns the cache off, since this
module would not reproduce it.

Correctness rests on Black's own rules, which this does not change:

* ``restore`` only names files whose current content hashes to the recorded
  value, and Black records that file's current stat and hash itself. Any edit
  reruns Black on the file;
* Black only caches files it found already formatted, so an unformatted file
  (every baseline entry included) is checked on every run;
* the cache file name embeds Black's mode key (target versions, line length,
  string normalisation, preview features), and ``restore`` refuses a saved
  cache whose file name is not the current mode's, so a ``[tool.black]`` change
  starts cold;
* entries are stored per Black version, and ``restore`` refuses a JSON written
  by any version other than the installed one, so a pin bump starts cold too.

Every failure is a warning and an exit 0, because a missing or rejected cache
only costs a cold run, never a verdict. That includes a Black release whose
``black.cache`` API changed shape: any missing name, changed signature or
unexpected result starts cold instead of crashing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FORMAT = 2
CACHE_FILE = re.compile(r"^cache\.[A-Za-z0-9.,@_-]+\.pickle$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# [tool.black] keys that cannot change black.Mode's cache key. line-length and
# target-version are mapped into the mode below; anything else might, so it
# turns the cache off rather than risk a mode this module does not reproduce.
MODE_NEUTRAL_KEYS = frozenset(
    {
        "line_length",
        "target_version",
        "force_exclude",
        "extend_exclude",
        "exclude",
        "include",
        "required_version",
    }
)


class ColdStart(Exception):
    """The cache cannot be used; the run proceeds without it."""


def _warn(message: str) -> None:
    print(f"::warning::black cache: {message}; starting cold")


def _gate_target_version(root: Path) -> str:
    script = root / "scripts" / "check_black_formatting.py"
    spec = importlib.util.spec_from_file_location("check_black_formatting", script)
    if spec is None or spec.loader is None:
        raise ColdStart(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.TARGET_VERSION)


def _black(root: Path) -> tuple[Any, Any, Path]:
    """Black's cache class, the gate's mode and Black's cache directory.

    Raises ColdStart for anything this module cannot use as expected.
    """
    if not os.environ.get("BLACK_CACHE_DIR"):
        raise ColdStart("BLACK_CACHE_DIR is unset")
    try:
        import black
        import black.cache as cache

        config = black.parse_pyproject_toml(str(root / "pyproject.toml"))
        unknown = sorted(set(config) - MODE_NEUTRAL_KEYS)
        if unknown:
            raise ColdStart(f"[tool.black] sets {unknown}, which this module does not map")
        mode = black.Mode(
            target_versions={black.TargetVersion[_gate_target_version(root).upper()]},
            line_length=int(config.get("line_length", black.DEFAULT_LINE_LENGTH)),
        )
        # Black's own layout: $BLACK_CACHE_DIR/<black version>.
        version_dir = Path(cache.get_cache_dir())
        return cache.Cache, mode, version_dir
    except ColdStart:
        raise
    except Exception as exc:  # noqa: BLE001 - any API mismatch is a cold run
        raise ColdStart(f"black's API is not the expected shape ({type(exc).__name__}: {exc})")


def _read(cache_cls: Any, mode: Any, version_dir: Path) -> Any:
    cache = cache_cls.read(mode, version_dir)
    if not isinstance(cache.file_data, dict):
        raise ColdStart("Cache.read returned no file table")
    if Path(cache.cache_file).parent != version_dir or not CACHE_FILE.match(
        Path(cache.cache_file).name
    ):
        raise ColdStart(f"Cache.read chose {cache.cache_file}")
    return cache


def _valid_relative(path: object) -> bool:
    if not isinstance(path, str) or not path or "\\" in path:
        return False
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts


def _write_payload(dest: Path, payload: dict[str, Any]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"format": FORMAT, **payload}, sort_keys=True), encoding="utf-8")


def export(dest: Path, root: Path = ROOT) -> int:
    root = root.resolve()
    try:
        cache_cls, mode, version_dir = _black(root)
        cache = _read(cache_cls, mode, version_dir)
        entries: dict[str, str] = {}
        for key, data in cache.file_data.items():
            try:
                relative = Path(key).relative_to(root).as_posix()
            except ValueError:
                continue
            digest = getattr(data, "hash", None)
            if _valid_relative(relative) and isinstance(digest, str) and SHA256.match(digest):
                entries[relative] = digest
    except Exception as exc:  # noqa: BLE001 - ColdStart and any API surprise
        _warn(str(exc) if isinstance(exc, ColdStart) else f"{type(exc).__name__}: {exc}")
        # An empty table, so the save step still has a file and the next run
        # starts cold rather than restoring an older table.
        _write_payload(dest, {"black": None, "cache_file": None, "files": {}})
        return 0
    _write_payload(
        dest,
        {"black": version_dir.name, "cache_file": Path(cache.cache_file).name, "files": entries},
    )
    print(f"black cache: exported {len(entries)} entr(y/ies) to {dest}")
    return 0


def restore(src: Path, root: Path = ROOT) -> int:
    root = root.resolve()
    if not src.is_file():
        print(f"black cache: no saved cache at {src}; this run starts cold")
        return 0
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            raise ColdStart("saved cache has an unknown format")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise ColdStart("saved cache is empty")
        cache_cls, mode, version_dir = _black(root)
        if payload.get("black") != version_dir.name:
            raise ColdStart(f"saved by black {payload.get('black')!r}, running {version_dir.name}")
        cache = _read(cache_cls, mode, version_dir)
        if payload.get("cache_file") != Path(cache.cache_file).name:
            raise ColdStart("saved for a different black mode (a [tool.black] change)")
        sources = []
        for relative, digest in files.items():
            if not (_valid_relative(relative) and isinstance(digest, str)):
                continue
            path = root / relative
            # Only a file whose bytes are the ones Black found formatted is
            # handed back to Black, which then records its stat and hash itself.
            if path.is_file() and cache_cls.hash_digest(path) == digest:
                sources.append(path)
        cache.write(sources)
    except Exception as exc:  # noqa: BLE001 - ColdStart and any API surprise
        message = str(exc) if isinstance(exc, ColdStart) else f"{type(exc).__name__}: {exc}"
        _warn(message)
        return 0
    print(f"black cache: restored {len(sources)} entr(y/ies) for {root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("export", "restore"):
        sub.add_parser(command).add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "export":
        return export(args.path)
    return restore(args.path)


if __name__ == "__main__":
    sys.exit(main())
