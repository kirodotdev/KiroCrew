"""``GET /api/sail/rule-index``: every Appian system rule file in a git repo.

Backs SAIL Cmd/Ctrl+click go-to-definition in the dashboard editor
(website/src/languages/sailNavigation.ts). Instead of walking the tree like
``/api/file-search`` (which is ceilinged and too slow on a repo the size of
``ae``), it asks git: ``git ls-files '*SYSTEM_SYSRULES_*.xml'`` answers in
~75ms for ~18k rules, is always current with the checkout, and needs no
background index to keep in sync. The browser caches the result per repo.

``?file=<abs path>`` names any file in the repo; the repo root is resolved
from it with ``git rev-parse --show-toplevel``. Owner-only, like every file
reader, and sensitive paths are refused.

Response: ``{"root": "<abs repo root>", "rules": {"<lowercased rule name>": "<abs path>"}}``.
"""

from __future__ import annotations

import asyncio
import os
import time

from aiohttp import web

_PATTERN = "*SYSTEM_SYSRULES_*.xml"
_PREFIX = "system_sysrules_"
_CACHE_TTL_SECS = 60.0
_GIT_TIMEOUT_SECS = 10.0

# repo root -> (built_at, rules)
_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def _git(cwd: str, *args: str, timeout: float = _GIT_TIMEOUT_SECS) -> bytes | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    return out if proc.returncode == 0 else None


async def api_sail_rule_index(request: web.Request) -> web.Response:
    from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
    from kiro_crew.security import is_sensitive_path

    denied = await require_owner_dashboard_request(request, "sail_rule_index")
    if denied is not None:
        return denied

    raw = request.query.get("file", "").strip()
    if not raw or not os.path.isabs(raw):
        return web.json_response({"error": "file must be an absolute path"}, status=400)
    if is_sensitive_path(raw):
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    start = raw if os.path.isdir(raw) else os.path.dirname(raw)
    if not os.path.isdir(start):
        return web.json_response({"error": "not found"}, status=404)

    top = await _git(start, "rev-parse", "--show-toplevel")
    if top is None:
        return web.json_response({"root": None, "rules": {}})
    root = top.decode("utf-8", "replace").strip()
    if is_sensitive_path(root):
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)

    _ensure_usage_index(root)
    jfn = _ensure_java_fn_index(root)
    functions = jfn.functions if jfn else {}
    hit = _cache.get(root)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECS:
        return web.json_response({"root": root, "rules": hit[1], "functions": functions, "functionsReady": jfn is not None})

    listing = await _git(root, "ls-files", "-z", "--", _PATTERN)
    rules: dict[str, str] = {}
    for rel in (listing or b"").split(b"\0"):
        if not rel:
            continue
        path = os.path.join(root, rel.decode("utf-8", "replace"))
        name = os.path.basename(path)[: -len(".xml")].lower()
        if name.startswith(_PREFIX):
            name = name[len(_PREFIX):]
        # First one wins on duplicate names (same rule in two libraries).
        rules.setdefault(name, path)
    _cache[root] = (time.monotonic(), rules)
    return web.json_response({"root": root, "rules": rules, "functions": functions, "functionsReady": jfn is not None})


_IDENT_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_USAGE_GLOBS = ("*.xml", "*.txt", "*.sail")
_USAGE_LIMIT = 500


async def api_sail_usages(request: web.Request) -> web.Response:
    """``GET /api/sail/usages?file=<abs path>&name=<rule name>``.

    Every line in the repo holding ``file`` that calls ``a!name`` or
    ``rule!name`` (case-insensitive, whole word), via ``git grep``. Response:
    ``{"root", "name", "usages": [{"path", "line", "text"}], "truncated"}``.
    """
    from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
    from kiro_crew.security import is_sensitive_path

    denied = await require_owner_dashboard_request(request, "sail_usages")
    if denied is not None:
        return denied
    raw = request.query.get("file", "").strip()
    name = request.query.get("name", "").strip()
    if not raw or not os.path.isabs(raw):
        return web.json_response({"error": "file must be an absolute path"}, status=400)
    # A bare SAIL identifier only: it is passed to git as a fixed string, but
    # keep it tight anyway.
    if not name or len(name) > 200 or not set(name) <= _IDENT_OK:
        return web.json_response({"error": "name must be a SAIL identifier"}, status=400)
    if is_sensitive_path(raw):
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    start = raw if os.path.isdir(raw) else os.path.dirname(raw)
    if not os.path.isdir(start):
        return web.json_response({"error": "not found"}, status=404)
    top = await _git(start, "rev-parse", "--show-toplevel")
    if top is None:
        return web.json_response({"root": None, "name": name, "usages": [], "truncated": False})
    root = top.decode("utf-8", "replace").strip()
    if is_sensitive_path(root):
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)

    idx = _ensure_usage_index(root)
    if idx is not None:
        usages, truncated = await asyncio.to_thread(_lookup_usages, idx, name)
        return web.json_response({"root": root, "name": name, "usages": usages, "truncated": truncated, "indexed": True})

    # Index still building (first ~7s after a repo is first seen): direct grep.
    out = await _git(
        root, "grep", "-n", "-I", "-i", "-w", "-F", "--no-color",
        "-e", f"a!{name}", "-e", f"rule!{name}", "-e", f"fn!{name}", "--", *_USAGE_GLOBS,
    )
    usages: list[dict] = []
    truncated = False
    for raw_line in (out or b"").splitlines():
        # path:line:text  (paths in this repo carry no colons)
        parts = raw_line.decode("utf-8", "replace").split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        if len(usages) >= _USAGE_LIMIT:
            truncated = True
            break
        usages.append({"path": os.path.join(root, parts[0]), "line": int(parts[1]), "text": parts[2].strip()[:300]})
    return web.json_response({"root": root, "name": name, "usages": usages, "truncated": truncated})


# ---------------------------------------------------------------------------
# Reverse usage index -- the analogue of IntelliJ's word index (IdIndex) that
# the SAIL plugin feeds through SailFindUsagesProvider.getWordsScanner().
#
# ONE background pass per repo (`git grep -o` for every `a!name` / `rule!name`,
# ~7s on ae: ~670k references, ~31k files, ~20k names) builds
#     name (lowercased) -> packed [file_id, line, file_id, line, ...]
# so a lookup is a dict hit. The index is rebuilt in the background when it is
# older than _USAGE_TTL_SECS (stale-while-revalidate); at lookup the current
# line text is read from disk, and a file whose indexed line no longer carries
# the reference (it was edited) is re-scanned, so results stay accurate between
# rebuilds.
# ---------------------------------------------------------------------------

import re as _re
from array import array as _array

_USAGE_TTL_SECS = 120.0
_USAGE_BUILD_TIMEOUT_SECS = 120.0
_REF_PATTERN = r"(a|rule|fn)![A-Za-z_][A-Za-z0-9_]*"


class _UsageIndex:
    __slots__ = ("root", "files", "refs", "built_at")

    def __init__(self, root: str, files: list[str], refs: dict[str, "_array"]) -> None:
        self.root = root
        self.files = files
        self.refs = refs
        self.built_at = time.monotonic()


_usage_indexes: dict[str, _UsageIndex] = {}
_usage_builds: dict[str, "asyncio.Task[None]"] = {}


def _parse_refs(root: str, out: bytes) -> _UsageIndex:
    files: list[str] = []
    file_ids: dict[bytes, int] = {}
    refs: dict[str, _array] = {}
    for ln in out.split(b"\n"):
        # path:line:match   (git grep -n -o)
        parts = ln.split(b":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        fid = file_ids.get(parts[0])
        if fid is None:
            fid = file_ids[parts[0]] = len(files)
            files.append(os.path.join(root, parts[0].decode("utf-8", "replace")))
        bang = parts[2].find(b"!")
        if bang < 0:
            continue
        name = parts[2][bang + 1:].decode("ascii", "replace").lower()
        arr = refs.get(name)
        if arr is None:
            arr = refs[name] = _array("I")
        arr.append(fid)
        arr.append(int(parts[1]))
    return _UsageIndex(root, files, refs)


async def _build_usage_index(root: str) -> None:
    try:
        out = await _git(
            root, "grep", "-n", "-o", "-I", "-i", "-E", _REF_PATTERN, "--", *_USAGE_GLOBS,
            timeout=_USAGE_BUILD_TIMEOUT_SECS,
        )
        if out is not None:
            _usage_indexes[root] = await asyncio.to_thread(_parse_refs, root, out)
    finally:
        _usage_builds.pop(root, None)


def _ensure_usage_index(root: str) -> _UsageIndex | None:
    """Current index for *root* (possibly stale); starts a background (re)build
    when there is none or it is older than the TTL. None until the first build lands."""
    idx = _usage_indexes.get(root)
    stale = idx is None or time.monotonic() - idx.built_at > _USAGE_TTL_SECS
    if stale and root not in _usage_builds:
        _usage_builds[root] = asyncio.get_running_loop().create_task(_build_usage_index(root))
    return idx


def _lookup_usages(idx: _UsageIndex, name: str) -> tuple[list[dict], bool]:
    """Blocking (reads files): resolve indexed refs to current {path, line, text}."""
    arr = idx.refs.get(name.lower())
    if not arr:
        return [], False
    by_file: dict[int, list[int]] = {}
    for i in range(0, len(arr), 2):
        by_file.setdefault(arr[i], []).append(arr[i + 1])
    word = _re.compile(r"(?<![A-Za-z0-9_])(?:a|rule|fn)!" + _re.escape(name) + r"(?![A-Za-z0-9_])", _re.IGNORECASE)
    usages: list[dict] = []
    truncated = False
    for fid, lines in by_file.items():
        path = idx.files[fid]
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read().split("\n")
        except OSError:
            continue  # deleted since the build
        hits = sorted({ln for ln in lines if 0 < ln <= len(text) and word.search(text[ln - 1])})
        if len(hits) != len(set(lines)):
            # Edited since the build: re-scan this one file.
            hits = [i + 1 for i, t in enumerate(text) if word.search(t)]
        for ln in hits:
            if len(usages) >= _USAGE_LIMIT:
                return usages, True
            usages.append({"path": path, "line": ln, "text": text[ln - 1].strip()[:300]})
    return usages, truncated


# ---------------------------------------------------------------------------
# Java-backed SAIL functions -- what the SAIL plugin's SystemFunctionDeclarationsCache
# collects through IntelliJ's Java PSI (ConstantRegisteredFnDeclarationCacher,
# PluginFnDeclarationCacher), done with git grep:
#   * `FN_NAME = "isValidNameLength"` constants (paired with
#     `FN_ID = new Id(Domain.SYS|FN, FN_NAME)`) and literal
#     `new Id(Domain.X, "name")` ids -> the Function subclass;
#   * `repository.register("name", ...)` in FunctionModules -> the register call;
#   * `@Function`-annotated plugin methods -> fn!<methodName>.
# name (lowercased) -> "abs path:line". Main sources win over test sources.
# Built in the background (~2s on ae) and refreshed after _JAVA_FN_TTL_SECS.
# NB: macOS git grep's ERE has no \s -- use [[:space:]].
# ---------------------------------------------------------------------------

_JAVA_FN_TTL_SECS = 600.0
_FN_NAME_RE = r'FN_NAME[[:space:]]*=[[:space:]]*"[A-Za-z_][A-Za-z0-9_]*"'
_FN_ID_LITERAL_RE = r'new Id\(Domain\.[A-Z_]+,[[:space:]]*"[A-Za-z_][A-Za-z0-9_]*"\)'
_ANNOTATION_RE = r'^[[:space:]]*@Function([[:space:]]|\(|$)'
# FunctionModule string-literal registration: repository.register("tointeger", new Caster(...))
_REGISTER_LITERAL_RE = r'\.register\([[:space:]]*"[A-Za-z_][A-Za-z0-9_]*"'
_py_quoted = _re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')
_py_method = _re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(')
_JAVA_KEYWORDS = frozenset({"public", "static", "final", "private", "protected", "synchronized", "new", "if", "for", "while", "return"})


class _JavaFnIndex:
    __slots__ = ("functions", "built_at")

    def __init__(self, functions: dict[str, str]) -> None:
        self.functions = functions
        self.built_at = time.monotonic()


_java_fn_indexes: dict[str, _JavaFnIndex] = {}
_java_fn_builds: dict[str, "asyncio.Task[None]"] = {}


def _put_fn(functions: dict[str, str], name: str, loc: str) -> None:
    key = name.lower()
    prev = functions.get(key)
    if prev is None or ("/src/test/" in prev and "/src/test/" not in loc):
        functions[key] = loc


def _parse_java_fns(root: str, constants: bytes, annotated: bytes) -> _JavaFnIndex:
    functions: dict[str, str] = {}
    for ln in constants.split(b"\n"):
        parts = ln.decode("utf-8", "replace").split(":", 2)  # path:line:text
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        m = _py_quoted.search(parts[2])
        if m:
            _put_fn(functions, m.group(1), f"{os.path.join(root, parts[0])}:{parts[1]}")
    # `git grep -n -A4`: match lines are path:N:text, context lines path-N-text,
    # groups separated by `--`. The method name is on the first following line
    # that looks like a declaration.
    pending: str | None = None
    for ln in annotated.split(b"\n"):
        text = ln.decode("utf-8", "replace")
        if text == "--":
            pending = None
            continue
        m = _re.match(r"^(.*?)([:-])(\d+)[:-](.*)$", text)
        if not m:
            continue
        path, sep, lineno, body = m.groups()
        if sep == ":" and "@Function" in body:
            pending = path
            continue
        if pending and path == pending and not body.strip().startswith("@"):
            for cand in _py_method.findall(body):
                if cand not in _JAVA_KEYWORDS:
                    _put_fn(functions, cand, f"{os.path.join(root, path)}:{lineno}")
                    break
            pending = None
    return _JavaFnIndex(functions)


async def _build_java_fn_index(root: str) -> None:
    try:
        constants = await _git(
            root, "grep", "-n", "-I", "-E", "-e", _FN_NAME_RE, "-e", _FN_ID_LITERAL_RE, "-e", _REGISTER_LITERAL_RE, "--", "*.java",
            timeout=_USAGE_BUILD_TIMEOUT_SECS,
        )
        annotated = await _git(
            root, "grep", "-n", "-I", "-A4", "-E", _ANNOTATION_RE, "--", "*.java",
            timeout=_USAGE_BUILD_TIMEOUT_SECS,
        )
        _java_fn_indexes[root] = await asyncio.to_thread(_parse_java_fns, root, constants or b"", annotated or b"")
    finally:
        _java_fn_builds.pop(root, None)


def _ensure_java_fn_index(root: str) -> _JavaFnIndex | None:
    idx = _java_fn_indexes.get(root)
    stale = idx is None or time.monotonic() - idx.built_at > _JAVA_FN_TTL_SECS
    if stale and root not in _java_fn_builds:
        _java_fn_builds[root] = asyncio.get_running_loop().create_task(_build_java_fn_index(root))
    return idx
