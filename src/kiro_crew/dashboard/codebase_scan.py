"""Scan a git work tree into the model a codebase session draws (PoC).

A codebase session's pane draws a work tree as a 3D model: each file sized by its
line count, folders as districts, imports as relationships. This module produces that
model: every file git knows about (tracked, plus untracked files that are not
ignored, so a refactor's new files appear before they are committed), its line
count, language and modification time, and import edges between files.

Imports are read with a plain line scanner, not a parser: Python ``import`` /
``from ... import`` and relative TS/JS ``import`` / ``export ... from`` /
``require`` / ``import()``. An import that names no file in the tree is dropped.

``sig`` digests HEAD, ``git status`` and the stat of every dirty file, so a caller
that passes the ``sig`` it last saw gets ``{"unchanged": true}`` without the tree
being walked. A real scan re-reads only files whose size or mtime moved.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import re
import stat
import subprocess  # nosec B404 - fixed git argv, no shell
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from kiro_crew.sandbox import run_limited, sandboxed_spawn_argv

#: Files read side by side on a cold scan.
_READ_THREADS = 8

#: Files beyond this many are left out (``truncated``), so one huge monorepo cannot
#: stall the scan or the browser drawing it.
MAX_FILES = 30000

#: A file larger than this is not read for lines or imports.
MAX_READ_BYTES = 1_000_000

#: Import edges beyond this many are left out.
MAX_EDGES = 150000

_GIT_TIMEOUT_SECS = 20

_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".css": "css",
    ".scss": "css",
    ".html": "html",
    ".md": "markdown",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "config",
    ".cfg": "config",
    ".ini": "config",
    ".sh": "shell",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".svg": "image",
    ".png": "image",
    ".jpg": "image",
    ".gif": "image",
}

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

_PY_FROM = re.compile(
    r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+\(?[ \t]*([\w \t,*]*)", re.M
)
_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)", re.M)
_JS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\(\s*|^\s*import\s+)['"](\.{1,2}/[^'"]+)['"]""",
    re.M,
)


class ScanError(Exception):
    """The root cannot be scanned; ``code`` is the API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _git(root: Path, *args: str) -> bytes:
    """Run one read-only git query against *root*, OS-sandboxed.

    The tree is the caller's to name and its content (``.git/config`` included) is
    agent-writable, so git runs inside the sandbox with a credential-scrubbed
    environment and the resource ceiling, never as a bare child of the gateway.
    No sandbox backend means no scan, not an unisolated spawn.
    """
    try:
        argv, env, cleanup = sandboxed_spawn_argv(["git", "-C", str(root), *args])
    except RuntimeError as exc:  # no sandbox backend and no explicit opt-in
        raise ScanError("sandbox_unavailable", str(exc)) from exc
    try:
        done = run_limited(  # nosec B603 - fixed argv, no shell
            argv,
            env=env,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScanError("git_failed", f"git {args[0]} failed: {exc}") from exc
    finally:
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)
    if done.returncode != 0:
        raise ScanError("not_a_git_tree", done.stderr.decode("utf-8", "replace").strip()[:300])
    return bytes(done.stdout)


def _resolve_root(root: str) -> Path:
    if not root or not os.path.isabs(os.path.expanduser(root)):
        raise ScanError("invalid_root", "root must be an absolute path")
    path = Path(os.path.expanduser(root)).resolve()
    if not path.is_dir():
        raise ScanError("invalid_root", "root is not a directory")
    top = _git(path, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
    return Path(top).resolve()


def _python_module_index(paths: list[str]) -> dict[str, str]:
    """Dotted module name -> file. A name two files could claim is left out."""
    index: dict[str, str | None] = {}
    for p in paths:
        if not p.endswith(".py"):
            continue
        parts = p[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        # Every suffix, so ``src/pkg/mod.py`` is found as ``pkg.mod`` too.
        for k in range(len(parts)):
            name = ".".join(parts[k:])
            if not name:
                continue
            index[name] = p if index.get(name, p) == p else None
    return {k: v for k, v in index.items() if v}


#: One import, as names to look up. Every ``each`` name that resolves is an edge; when
#: none does, the first ``chain`` name that resolves is. Read once per file version
#: (cached) and resolved on every scan, since what a name resolves to depends on
#: which files exist now.
ImportSpec = tuple[tuple[str, ...], tuple[str, ...]]


def _python_specs(path: str, text: str) -> list[ImportSpec]:
    """Dotted module names each import may name."""
    out: list[ImportSpec] = []
    pkg = path.split("/")[:-1]
    # Only lines that open with an import can hold one: matching those alone is
    # far cheaper than running the patterns over every file whole.
    text = "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith(("from ", "import "))
    )
    for m in _PY_FROM.finditer(text):
        dots, mod, names = m.group(1), m.group(2), m.group(3)
        if dots:
            base = pkg[: len(pkg) - (len(dots) - 1)] if len(dots) > 1 else pkg
            full = ".".join([*base, *([mod] if mod else [])])
        else:
            full = mod
        if not full:
            continue
        each = tuple(
            f"{full}.{n}"
            for n in (x.strip().split(" ")[0] for x in names.split(","))
            if n and n != "*"
        )
        out.append((each, (full,)))
    for m in _PY_IMPORT.finditer(text):
        for name in (x.strip() for x in m.group(1).split(",")):
            # ``import a.b.c`` reaches a.b.c, or the nearest package of it.
            parts = name.split(".")
            out.append(((), tuple(".".join(parts[:k]) for k in range(len(parts), 0, -1))))
    return out


def _js_specs(path: str, text: str) -> list[ImportSpec]:
    """Repo paths each relative import may name, most likely first."""
    out: list[ImportSpec] = []
    here = posixpath.dirname(path)
    text = "\n".join(
        line
        for line in text.splitlines()
        if "./" in line and ("import" in line or "from" in line or "require" in line)
    )
    for m in _JS_IMPORT.finditer(text):
        spec = posixpath.normpath(posixpath.join(here, m.group(1)))
        stem = spec
        for ext in _JS_EXTS:
            # ESM sources import ``./x.js`` for a ``./x.ts`` file.
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        chain = (spec, *(stem + e for e in _JS_EXTS), *(f"{spec}/index{e}" for e in _JS_EXTS))
        out.append(((), chain))
    return out


def _resolve(path: str, specs: list[ImportSpec], lookup: Any) -> set[str]:
    out: set[str] = set()
    for each, chain in specs:
        hits = {t for t in (lookup(n) for n in each) if t and t != path}
        if not hits:
            hits = next(({t} for t in (lookup(n) for n in chain) if t and t != path), set())
        out |= hits
    return out


def _line_count(raw: bytes) -> int:
    if not raw:
        return 0
    return raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)


#: Files a tool wrote, not a person: drawn low and grey, so a lock file's twenty
#: thousand lines do not read as the codebase's weirdest shape.
_GENERATED_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "Pipfile.lock",
    }
)
#: A data file this long is generated (a locale catalogue, a fixture dump).
_GENERATED_DATA_LINES = 3000


def _kind(path: str, lang: str, lines: int) -> str:
    name = posixpath.basename(path)
    if name in _GENERATED_NAMES or name.endswith((".min.js", ".min.css", ".map", ".lock")):
        return "generated"
    if lang in ("json", "yaml") and lines > _GENERATED_DATA_LINES:
        return "generated"
    return lang


#: Per work tree, what each file read as at a given size and mtime, so a rescan
#: re-reads only what changed. Bounded to a few trees; a scan replaces its tree's
#: entry whole, so a concurrent scan never sees a half-written one.
_CACHE: OrderedDict[str, dict[str, tuple[int, int, int, list[ImportSpec]]]] = OrderedDict()
_CACHE_TREES = 4


def _signature(top: Path) -> str:
    """Changes whenever the scan could: HEAD moves, or any file is dirtied or edited again."""
    digest = hashlib.sha256()
    try:
        digest.update(_git(top, "rev-parse", "HEAD"))
    except ScanError:
        pass  # no commit yet
    status = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    digest.update(status)
    # A dirty file edited again keeps its status line, so its stat goes in too.
    for entry in status.decode("utf-8", "replace").split("\0"):
        if len(entry) > 3 and entry[2] == " ":
            try:
                st = (top / entry[3:]).stat()
            except OSError:
                continue
            digest.update(
                f"{entry[3:]}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8", "replace")
            )
    return digest.hexdigest()


def _read_file(top: Path, p: str, size: int, lang: str, ext: str) -> tuple[int, list[ImportSpec]]:
    if size > MAX_READ_BYTES or lang == "image":
        return 0, []
    try:
        raw = (top / p).read_bytes()
    except OSError:
        return 0, []
    if b"\0" in raw[:8192]:
        return 0, []
    lines = _line_count(raw)
    if ext == ".py":
        return lines, _python_specs(p, raw.decode("utf-8", "replace"))
    if ext in _JS_EXTS:
        return lines, _js_specs(p, raw.decode("utf-8", "replace"))
    return lines, []


def scan(root: str, since: str | None = None) -> dict[str, Any]:
    """The model of the work tree containing *root*, or ``{"unchanged": True}``.

    ``files`` are rows ``[path, lines, lang index, mtime ms]`` with ``langs`` naming
    the indexes, and ``edges`` are ``[from row, to row]``: a large tree is tens of
    thousands of files, and named fields would double the payload.
    """
    top = _resolve_root(root)
    sig = _signature(top)
    if since and since == sig:
        return {"unchanged": True, "sig": sig}
    listed = _git(top, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    paths = sorted({p for p in listed.decode("utf-8", "replace").split("\0") if p})
    truncated = len(paths) > MAX_FILES
    paths = paths[:MAX_FILES]

    previous = _CACHE.get(str(top), {})
    cache: dict[str, tuple[int, int, int, list[ImportSpec]]] = {}
    langs: list[str] = []
    lang_index: dict[str, int] = {}
    listing: list[tuple[str, os.stat_result, str, str]] = []
    for p in paths:
        try:
            st = (top / p).stat()
        except OSError:
            continue  # deleted from the tree, not yet from the index
        if not stat.S_ISREG(st.st_mode):
            continue
        ext = posixpath.splitext(p)[1].lower()
        listing.append((p, st, _LANG_BY_EXT.get(ext, "other"), ext))

    def read(item: tuple[str, os.stat_result, str, str]) -> tuple[int, list[ImportSpec]]:
        p, st, lang, ext = item
        hit = previous.get(p)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2], hit[3]
        return _read_file(top, p, st.st_size, lang, ext)

    # Opening a file is most of a cold scan's cost, and it waits on the OS, so
    # the reads run side by side.
    with ThreadPoolExecutor(max_workers=_READ_THREADS) as pool:
        read_back = list(pool.map(read, listing))

    rows: list[list[Any]] = []
    for (p, st, lang, _ext), (lines, specs) in zip(listing, read_back):
        cache[p] = (st.st_size, st.st_mtime_ns, lines, specs)
        lang = _kind(p, lang, lines)
        if lang not in lang_index:
            lang_index[lang] = len(langs)
            langs.append(lang)
        rows.append([p, lines, lang_index[lang], st.st_mtime_ns // 1_000_000])
    _CACHE[str(top)] = cache
    _CACHE.move_to_end(str(top))
    while len(_CACHE) > _CACHE_TREES:
        _CACHE.popitem(last=False)

    present = [r[0] for r in rows]
    file_set = set(present)
    modules = _python_module_index(present)
    index_of = {p: i for i, p in enumerate(present)}
    edges: list[list[int]] = []
    for p in present:
        specs = cache[p][3]
        if not specs:
            continue
        lookup = modules.get if p.endswith(".py") else (lambda n: n if n in file_set else None)
        edges.extend([index_of[p], index_of[t]] for t in sorted(_resolve(p, specs, lookup)))
        if len(edges) >= MAX_EDGES:
            break

    branch = _git(top, "rev-parse", "--abbrev-ref", "HEAD").decode("utf-8", "replace").strip()
    try:
        head = _git(top, "rev-parse", "--short", "HEAD").decode("utf-8", "replace").strip()
    except ScanError:
        head = ""  # a fresh repo with no commit yet
    return {
        "root": str(top),
        "branch": branch,
        "head": head,
        "sig": sig,
        "langs": langs,
        "files": rows,
        "edges": edges[:MAX_EDGES],
        "truncated": truncated,
    }
