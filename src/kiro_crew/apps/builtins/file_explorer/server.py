"""File Explorer backend for KiroCrew.

A small stdlib-only HTTP server that exposes a read-only filesystem API for the
file-explorer frontend. Bound to localhost; KiroCrew proxies requests from
``/apps/file-explorer/api/*`` to this process.

Endpoints (all relative to the proxied base — the server itself sees them at
the root since KiroCrew strips the prefix):

  GET  /health                                  → {"status": "ok"}
  GET  /resolve?path=<p>                        → {"path", "exists", "type", "size", "mtime"}
  GET  /tree?path=<p>&depth=<n>                 → [{"name","path","type","size","mtime","gitStatus?"}]
  GET  /read?path=<p>&max_bytes=<n>             → {"path","size","mtime","encoding","content","truncated","mime","binary"}
  GET  /search?path=<p>&q=<query>&include=...   → [{"file","line","col","preview"}]
  GET  /git-status?path=<p>                     → {"repoRoot","branch","statuses":{relpath: code}}
  GET  /complete?path=<p>&kind=<dir|all>&limit=<n> → {"parent","prefix","entries":[...]}

Path safety: callers may only access paths under ``$HOME``, ``/home/``,
``/tmp/``, or ``/opt/`` (after symlink resolution).  Paths outside the
allow-list return 403.

Size / depth caps are tunable via env vars but have safe defaults.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kiro_crew.apps.proxy_auth import verify_proxy_request
from kiro_crew.hooks import safe_read_file_bytes
from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "file-explorer")

# Read size cap (bytes)
MAX_READ_BYTES = int(os.environ.get("FE_MAX_READ_BYTES", 4 * 1024 * 1024))  # 4 MB
MAX_TREE_ENTRIES = int(os.environ.get("FE_MAX_TREE_ENTRIES", 5000))
MAX_SEARCH_RESULTS = int(os.environ.get("FE_MAX_SEARCH_RESULTS", 500))
SEARCH_TIMEOUT_SEC = int(os.environ.get("FE_SEARCH_TIMEOUT_SEC", 15))
GIT_TIMEOUT_SEC = int(os.environ.get("FE_GIT_TIMEOUT_SEC", 5))

# Allow-list of root paths (resolved). Anything outside is denied.
_HOME_RAW = os.environ.get("HOME", "")
if not _HOME_RAW or Path(_HOME_RAW).resolve() == Path("/"):
    _HOME = Path("/tmp").resolve()
else:
    _HOME = Path(_HOME_RAW).resolve()
ALLOWED_ROOTS = [
    _HOME,
    Path("/home").resolve(),
    Path("/tmp").resolve(),
    Path("/opt").resolve() if Path("/opt").exists() else _HOME,
]
# De-dupe and only keep ones that exist
ALLOWED_ROOTS = list({p for p in ALLOWED_ROOTS if p.exists()})

# Default ignore patterns for tree + search
IGNORE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "venv",
    ".venv",
    "env",
    ".env",
    "dist",
    "build",
    "out",
    ".next",
    "target",
    ".gradle",
    ".idea",
    ".vscode",
    ".cache",
    "bower_components",
    ".tox",
    ".eggs",
    "htmlcov",
    ".coverage",
    ".DS_Store",
}

# Sensitive credential directories — filtered from tree/search/complete
SENSITIVE_DIRS = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".docker",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
}

# .kirocrew subdirectories that are safe for the file explorer to access.
# Everything else under .kirocrew/ is blocked (keys, tokens, DB, sessions).
_KIROCREW_SAFE_SUBDIRS = {
    "workspace", "uploads", "skills", "artifacts", "apps",
    "app-sources", "workflows", "pods", "logs", "crons",
}

# Binary extensions short-circuited from /read content fetch
BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".tiff",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".rar",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".class",
    ".jar",
    ".war",
    ".o",
    ".a",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".mkv",
    ".webm",
    ".sqlite",
    ".db",
    ".duckdb",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".eot",
}

# Image extensions get a special mime hint
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}

logger = logging.getLogger(APP_NAME)
logging.basicConfig(
    level=logging.INFO,
    format=f"[{APP_NAME}] %(asctime)s %(levelname)s %(message)s",
)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class PathError(Exception):
    """Raised for invalid or denied paths."""

    def __init__(self, msg: str, status: int = 400) -> None:
        super().__init__(msg)
        self.status = status


def _expand(p: str) -> Path:
    """Expand ~/ and resolve to an absolute Path (without requiring existence)."""
    if not p:
        raise PathError("path is required", 400)
    return Path(os.path.expanduser(p)).resolve()


def _safe_path(raw: str, must_exist: bool = True) -> Path:
    """Return a resolved Path that lives under an allowed root.

    Raises PathError(403) if the path escapes the allow-list, or PathError(404)
    if must_exist and the file/dir is missing.
    """
    p = _expand(raw)
    in_allow = any(_is_within(p, root) for root in ALLOWED_ROOTS)
    if not in_allow:
        raise PathError(f"path not allowed: {p}", 403)
    if _is_sensitive(p):
        raise PathError("access denied: sensitive path", 403)
    if must_exist and not p.exists():
        raise PathError(f"not found: {p}", 404)
    return p


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_sensitive(p: Path) -> bool:
    """Check if path is a sensitive credential location.

    Uses both the shared ``is_sensitive_path()`` helper AND the module-level
    ``SENSITIVE_DIRS`` set so the access-deny gate can never be narrower than
    the listing-hide filter.

    For ``.kirocrew/``, a granular policy applies: only subdirectories listed in
    ``_KIROCREW_SAFE_SUBDIRS`` are accessible; everything else (keys, tokens,
    DB, sessions) is blocked.
    """
    if any(part in SENSITIVE_DIRS for part in p.parts):
        return True
    # Granular .kirocrew policy: block unless the path descends into a safe subdir.
    if ".kirocrew" in p.parts:
        idx = list(p.parts).index(".kirocrew")
        # .kirocrew/ root itself is blocked (deny-by-default). Listing endpoints
        # use _kirocrew_safe_children() to enumerate only safe subdirs.
        if idx + 1 >= len(p.parts):
            return True
        next_part = p.parts[idx + 1]
        if next_part not in _KIROCREW_SAFE_SUBDIRS:
            return True
    return is_sensitive_path(str(p))


def _kirocrew_safe_children(kirocrew_dir: Path) -> list[dict]:
    """Return entry metadata for only the safe subdirs of a .kirocrew/ directory.

    This is the single enforcement point for deny-by-default .kirocrew listing:
    callers get back only entries that are in ``_KIROCREW_SAFE_SUBDIRS``.
    Sensitive file names (config.json, *.key, memory.db) are never exposed.
    """
    out: list[dict] = []
    try:
        children = sorted(kirocrew_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except (OSError, PermissionError):
        return out
    for child in children:
        if child.name in _KIROCREW_SAFE_SUBDIRS and child.is_dir():
            out.append(_entry_meta(child))
    return out


def _sel_audit(operation: str, resources: str, outcome: str = "granted") -> None:
    """Emit SEL audit event for file access operations."""
    sel().log_api_access(
        caller="file-explorer",
        operation=operation,
        outcome=outcome,
        source="builtin-app",
        resources=resources,
    )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _file_kind(p: Path) -> str:
    # Follow symlinks: a symlink to a dir/file should behave like its target
    # so the UI navigates into it instead of trying to read it as a file.
    if p.is_dir():
        return "dir"
    if p.is_file():
        return "file"
    if p.is_symlink():
        return "symlink"  # broken/dangling symlink
    return "other"


def _entry_meta(p: Path, *, with_size: bool = True) -> dict:
    try:
        st = p.stat()  # follow symlinks -- consistent with _file_kind()
    except OSError:
        return {
            "name": p.name,
            "path": str(p),
            "type": "missing",
            "size": 0,
            "mtime": 0,
        }
    kind = _file_kind(p)
    info = {
        "name": p.name,
        "path": str(p),
        "type": kind,
        "mtime": int(st.st_mtime),
    }
    if with_size and kind == "file":
        info["size"] = st.st_size
    elif kind == "dir":
        info["size"] = 0
        # Cheap probe: a directory with .git/ is a git repo root.
        try:
            if (p / ".git").exists():
                info["isGitRoot"] = True
        except OSError:
            pass
    else:
        info["size"] = st.st_size if stat.S_ISREG(st.st_mode) else 0
    return info


def _list_dir(p: Path, depth: int = 1, ignore: bool = True) -> tuple[list[dict], bool]:
    """List directory contents up to ``depth`` levels (1 = immediate children).
    Returns (entries, truncated)."""
    out: list[dict] = []
    count = [0]

    def walk(d: Path, rem: int) -> list[dict]:
        if count[0] >= MAX_TREE_ENTRIES:
            return []
        try:
            entries = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (OSError, PermissionError) as exc:
            return [{"name": d.name, "path": str(d), "type": "error", "error": str(exc)}]
        items: list[dict] = []
        for child in entries:
            if count[0] >= MAX_TREE_ENTRIES:
                items.append(
                    {"name": "...", "path": str(d), "type": "truncated", "size": 0, "mtime": 0}
                )
                break
            if child.name in SENSITIVE_DIRS:
                continue
            # Deny-by-default for .kirocrew root listings: show ONLY the safe
            # subdirs. Even exposing the *names* of credential material
            # (config.json, *.key, memory.db) is an information leak.
            if d.name == ".kirocrew" and child.name not in _KIROCREW_SAFE_SUBDIRS:
                continue
            if ignore and child.name in IGNORE_DIRS:
                continue
            count[0] += 1
            meta = _entry_meta(child)
            if rem > 1 and meta["type"] == "dir":
                resolved_child = child.resolve()
                if any(_is_within(resolved_child, root) for root in ALLOWED_ROOTS):
                    meta["children"] = walk(child, rem - 1)
            items.append(meta)
        return items

    out = walk(p, depth)
    return out, count[0] >= MAX_TREE_ENTRIES


def _is_binary_file(p: Path) -> bool:
    if p.suffix.lower() in BINARY_EXTS:
        return True
    # Sniff first 8KB for NUL bytes
    try:
        chunk = safe_read_file_bytes(str(p))
        if chunk is None:
            return False
        if b"\x00" in chunk[:8192]:
            return True
    except OSError:
        return False
    return False


_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".ts": "text/typescript",
    ".tsx": "text/jsx",
    ".jsx": "text/jsx",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
}


def _guess_mime(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(str(p))
    return mime or "text/plain"


# ---------------------------------------------------------------------------
# Git status
# ---------------------------------------------------------------------------


def _git_repo_root(p: Path) -> Path | None:
    """Walk up to find a .git directory."""
    for cand in [p] + list(p.parents):
        if (cand / ".git").exists():
            return cand
    return None


def _git_status(repo_root: Path) -> dict:
    """Return ``{branch, statuses}`` for a git repo."""
    out: dict = {"repoRoot": str(repo_root), "branch": "", "statuses": {}}
    try:
        cmd_branch, _ = wrap_argv(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"]
        )
        cmd_branch = cgroup_scope_argv(cmd_branch)  # cgroup DoS ceiling
        branch = subprocess.run(
            cmd_branch,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
            preexec_fn=resource_limit_preexec(),
        )
        if branch.returncode == 0:
            out["branch"] = branch.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["branch_error"] = str(exc)
    try:
        cmd_status, _ = wrap_argv(["git", "-C", str(repo_root), "status", "--porcelain=1", "-z"])
        cmd_status = cgroup_scope_argv(cmd_status)  # cgroup DoS ceiling
        proc = subprocess.run(
            cmd_status,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
            preexec_fn=resource_limit_preexec(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["status_error"] = str(exc)
        return out
    if proc.returncode != 0:
        out["status_error"] = proc.stderr.strip() or f"git exit {proc.returncode}"
        return out
    # -z output is NUL-delimited records.  Each record begins with 2 status
    # chars followed by a space, then the path.  Renames have a 2nd path that
    # follows after another NUL.
    raw = proc.stdout
    statuses: dict[str, str] = {}
    i = 0
    while i < len(raw):
        nl = raw.find("\x00", i)
        if nl < 0:
            break
        record = raw[i:nl]
        if len(record) >= 3:
            code = record[:2].strip() or record[:2]
            path = record[3:]
            statuses[path] = code
            # Skip the rename source path that follows
            if record[0] in ("R", "C"):
                nl2 = raw.find("\x00", nl + 1)
                if nl2 > 0:
                    nl = nl2
        i = nl + 1
    out["statuses"] = statuses
    return out


def _has_rg() -> bool:
    return shutil.which("rg") is not None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search(root: Path, query: str, include: str = "", exclude: str = "") -> list[dict]:
    """Recursive content search.  Uses ripgrep when available, falls back to
    a Python implementation for portability.  Returns results in ripgrep order
    (per-file).
    """
    if not query:
        return []
    if _has_rg():
        return _search_rg(root, query, include, exclude)
    return _search_python(root, query, include, exclude)


def _search_rg(root: Path, query: str, include: str, exclude: str) -> list[dict]:
    cmd = [
        "rg",
        "--json",
        "--max-count",
        "20",
        "--no-messages",
        "--smart-case",
        "--hidden",
    ]
    for ig in IGNORE_DIRS:
        cmd += ["--glob", f"!**/{ig}"]
    if include:
        for g in include.split(","):
            g = g.strip()
            if g:
                cmd += ["--glob", g]
    if exclude:
        for g in exclude.split(","):
            g = g.strip()
            if g:
                cmd += ["--glob", f"!{g}"]
    # Sensitive exclusions LAST — always enforced, cannot be overridden by user globs
    for sd in SENSITIVE_DIRS:
        cmd += ["--glob", f"!**/{sd}"]
    # .kirocrew handling. NOTE: in ripgrep, the presence of ANY non-negated
    # --glob turns the glob set into an allowlist (only matching files are
    # searched), so we must use ONLY negated globs here or every file outside
    # .kirocrew would be silently excluded from results.
    #  - Root inside a .kirocrew safe subdir: no glob needed — the request-path
    #    gate (_is_sensitive) already confirmed the subtree is safe.
    #  - Root outside .kirocrew: exclude the whole .kirocrew tree
    #    (deny-by-default; safe subdirs are reachable by searching them
    #    directly, which takes the branch above).
    if ".kirocrew" not in root.parts:
        cmd += ["--glob", "!**/.kirocrew/**"]
    cmd += ["--", query, str(root)]
    try:
        wrapped_cmd, _ = wrap_argv(cmd)
        wrapped_cmd = cgroup_scope_argv(wrapped_cmd)  # cgroup DoS ceiling
        proc = subprocess.run(
            wrapped_cmd,
            capture_output=True,
            text=True,
            timeout=SEARCH_TIMEOUT_SEC,
            check=False,
            preexec_fn=resource_limit_preexec(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("rg failed: %s — falling back to python", exc)
        return _search_python(root, query, include, exclude)
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        if len(out) >= MAX_SEARCH_RESULTS:
            break
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data") or {}
        path = (data.get("path") or {}).get("text") or ""
        if is_sensitive_path(path):
            continue
        line_no = data.get("line_number", 0)
        text = (data.get("lines") or {}).get("text") or ""
        # ripgrep gives column as bytes; we want the char column
        col = 0
        submatches = data.get("submatches") or []
        if submatches:
            col = submatches[0].get("start", 0) + 1
        out.append(
            {
                "file": path,
                "line": int(line_no),
                "col": int(col),
                "preview": text.rstrip("\n")[:400],
            }
        )
    return out


def _search_python(root: Path, query: str, include: str, exclude: str) -> list[dict]:
    deadline = time.monotonic() + SEARCH_TIMEOUT_SEC
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    inc_pats = [g.strip() for g in include.split(",") if g.strip()] if include else []
    exc_pats = [g.strip() for g in exclude.split(",") if g.strip()] if exclude else []

    def _matches_globs(p: Path, globs: list[str]) -> bool:
        return any(p.match(g) or p.name == g for g in globs)

    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > deadline:
            break
        # In-place prune ignored dirs
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and d not in SENSITIVE_DIRS]
        # .kirocrew deny-by-default: match rg behavior.
        # - If search root is OUTSIDE .kirocrew: skip entirely (rg blocks all).
        # - If search root is INSIDE a safe subdir: allow safe subdirs only.
        dp = Path(dirpath)
        if dp.name == ".kirocrew":
            if ".kirocrew" not in root.parts:
                # Root is outside — deny-by-default, match rg behavior
                dirnames[:] = []
            else:
                # Root is inside .kirocrew — allow safe subdirs only
                dirnames[:] = [d for d in dirnames if d in _KIROCREW_SAFE_SUBDIRS]
            filenames[:] = []  # never surface .kirocrew root files
        for fn in filenames:
            if len(out) >= MAX_SEARCH_RESULTS:
                return out
            fp = Path(dirpath) / fn
            if inc_pats and not _matches_globs(fp, inc_pats):
                continue
            if exc_pats and _matches_globs(fp, exc_pats):
                continue
            if fp.suffix.lower() in BINARY_EXTS:
                continue
            if _is_sensitive(fp):
                continue
            try:
                raw = safe_read_file_bytes(str(fp))
                if raw is None:
                    continue
                for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    m = pattern.search(line)
                    if m:
                        out.append(
                            {
                                "file": str(fp),
                                "line": n,
                                "col": m.start() + 1,
                                "preview": line.rstrip("\n")[:400],
                            }
                        )
                        if len(out) >= MAX_SEARCH_RESULTS:
                            return out
                        break  # one match per file is enough for v1 list
            except OSError:
                continue
    return out


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class FileExplorerHandler(BaseHTTPRequestHandler):
    server_version = "KiroCrew-FileExplorer/0.1"

    # Silence default logging — we use our own.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    # ----- routing -----
    def _authorized_or_health(self, method: str) -> bool:
        """Verify the gateway's X-KiroCrew-Proxy HMAC before dispatch (CWE-306).

        The health endpoint stays unauthenticated because the gateway's own
        liveness probe (apps/backend.py) hits the backend directly, unsigned.
        A read-only GET carries no body, so the signed body hash is sha256(b"").
        """
        route = urllib.parse.urlparse(self.path).path.rstrip("/")
        if route in ("", "/health", "/api", "/api/health"):
            return True
        if verify_proxy_request(
            self.headers.get("X-KiroCrew-Proxy", ""),
            method=method,
            target=self.path,
            body=b"",
        ):
            return True
        _sel_audit("proxy_auth_failed", self.path, outcome="denied")
        self._json(401, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized_or_health("GET"):
            return
        try:
            self._dispatch("GET")
        except PathError as exc:
            if exc.status == 403:
                _sel_audit("access_denied", self.path, outcome="denied")
            self._json(exc.status, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            corr = uuid.uuid4().hex[:12]
            logger.exception("GET %s failed [%s]", self.path, corr)
            # Generic body + correlation id — do not echo raw exception text to
            # the (reverse-proxied, browser-facing) client (CWE-209).
            self._json(500, {"error": "internal error", "id": corr})

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        route = url.path.rstrip("/") or "/"
        # KiroCrew's reverse proxy forwards requests as /api/<path>;
        # strip it so both direct and proxied requests resolve.
        if route.startswith("/api/"):
            route = route[4:] or "/"
        elif route == "/api":
            route = "/"

        if method == "GET" and route in ("/", "/health"):
            return self._json(
                200,
                {
                    "status": "ok",
                    "app": APP_NAME,
                    "version": "0.2.0",
                    "rg": _has_rg(),
                    "allowedRoots": [str(p) for p in ALLOWED_ROOTS],
                    "maxReadBytes": MAX_READ_BYTES,
                },
            )

        if method == "GET" and route == "/resolve":
            return self._h_resolve(qs)
        if method == "GET" and route == "/tree":
            return self._h_tree(qs)
        if method == "GET" and route == "/read":
            return self._h_read(qs)
        if method == "GET" and route == "/search":
            return self._h_search(qs)
        if method == "GET" and route == "/git-status":
            return self._h_git_status(qs)
        if method == "GET" and route == "/complete":
            return self._h_complete(qs)
        return self._json(404, {"error": f"{method} {route} not found"})

    # ----- handlers -----
    def _h_resolve(self, qs: dict[str, list[str]]) -> None:
        raw = (qs.get("path") or [""])[0]
        p = _safe_path(raw, must_exist=False)
        _sel_audit("resolve", str(p))
        meta = (
            _entry_meta(p)
            if p.exists()
            else {
                "name": p.name,
                "path": str(p),
                "type": "missing",
                "size": 0,
                "mtime": 0,
            }
        )
        meta["exists"] = p.exists()
        return self._json(200, meta)

    def _h_tree(self, qs: dict[str, list[str]]) -> None:
        raw = (qs.get("path") or [""])[0]
        depth = int((qs.get("depth") or ["1"])[0])
        depth = max(1, min(depth, 4))
        ignore = (qs.get("ignore") or ["1"])[0] not in {"0", "false"}
        # Special case: .kirocrew/ root — deny-by-default blocks it in _safe_path,
        # but we expose only safe children via the dedicated helper.
        # SECURITY: still enforce ALLOWED_ROOTS to prevent path traversal via
        # crafted paths like /tmp/attacker/.kirocrew or symlinks.
        expanded = _expand(raw)
        if expanded.name == ".kirocrew" and expanded.exists() and expanded.is_dir():
            # Resolve symlinks before checking ALLOWED_ROOTS — prevents a symlink
            # named .kirocrew (inside allowed root) pointing outside.
            resolved = expanded.resolve()
            if not any(_is_within(resolved, root) for root in ALLOWED_ROOTS):
                _sel_audit("tree_list", str(expanded), outcome="denied")
                raise PathError(f"path not allowed: {expanded}", 403)
            _sel_audit("tree_list", str(resolved))
            entries = _kirocrew_safe_children(resolved)
            return self._json(200, {"path": str(resolved), "entries": entries, "truncated": False})
        p = _safe_path(raw, must_exist=True)
        if not p.is_dir():
            raise PathError(f"not a directory: {p}", 400)
        _sel_audit("tree_list", str(p))
        entries, truncated = _list_dir(p, depth=depth, ignore=ignore)
        return self._json(
            200,
            {
                "path": str(p),
                "entries": entries,
                "truncated": truncated,
            },
        )

    def _h_read(self, qs: dict[str, list[str]]) -> None:
        raw = (qs.get("path") or [""])[0]
        max_bytes = int((qs.get("max_bytes") or [str(MAX_READ_BYTES)])[0])
        max_bytes = max(1, min(max_bytes, MAX_READ_BYTES))
        p = _safe_path(raw, must_exist=True)
        if not p.is_file():
            raise PathError(f"not a regular file: {p}", 400)
        _sel_audit("file_read", str(p))
        st = p.stat()
        mime = _guess_mime(p)
        is_image = p.suffix.lower() in IMAGE_EXTS
        is_binary = _is_binary_file(p) and not is_image  # we still let images through as base64
        body: dict = {
            "path": str(p),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "mime": mime,
            "binary": is_binary,
            "truncated": False,
            "encoding": "utf-8",
            "content": "",
        }
        if is_binary:
            return self._json(200, body)
        # Image: base64 (small images only)
        if is_image and st.st_size <= max_bytes:
            raw_img = safe_read_file_bytes(str(p))
            if raw_img is None:
                raise PathError("access denied", 403)
            body["encoding"] = "base64"
            body["content"] = base64.b64encode(raw_img).decode("ascii")
            return self._json(200, body)
        if is_image:
            # Too large to inline as base64
            body["binary"] = True
            return self._json(200, body)
        # Text: utf-8 with replacement, optional truncation
        try:
            raw_bytes = safe_read_file_bytes(str(p))
            if raw_bytes is None:
                raise PathError("access denied", 403)
            truncated = len(raw_bytes) > max_bytes
            if truncated:
                raw_bytes = raw_bytes[:max_bytes]
            body["truncated"] = truncated
            body["content"] = raw_bytes.decode("utf-8", errors="replace")
        except OSError as exc:
            raise PathError(f"read failed: {exc}", 500) from exc
        return self._json(200, body)

    def _h_search(self, qs: dict[str, list[str]]) -> None:
        raw = (qs.get("path") or [""])[0]
        q = (qs.get("q") or [""])[0]
        include = (qs.get("include") or [""])[0]
        exclude = (qs.get("exclude") or [""])[0]
        p = _safe_path(raw, must_exist=True)
        if not p.is_dir():
            raise PathError(f"not a directory: {p}", 400)
        if not q.strip():
            return self._json(200, {"results": [], "engine": "rg" if _has_rg() else "python"})
        _sel_audit("file_search", f"{p} q={q}")
        results = _search(p, q, include=include, exclude=exclude)
        return self._json(
            200,
            {
                "results": results,
                "engine": "rg" if _has_rg() else "python",
                "truncated": len(results) >= MAX_SEARCH_RESULTS,
            },
        )

    def _h_git_status(self, qs: dict[str, list[str]]) -> None:
        raw = (qs.get("path") or [""])[0]
        p = _safe_path(raw, must_exist=True)
        _sel_audit("git_status", str(p))
        repo = _git_repo_root(p if p.is_dir() else p.parent)
        if not repo:
            return self._json(200, {"repoRoot": "", "branch": "", "statuses": {}})
        if not any(_is_within(repo, root) for root in ALLOWED_ROOTS):
            raise PathError(f"git repo root not allowed: {repo}", 403)
        return self._json(200, _git_status(repo))

    def _h_complete(self, qs: dict[str, list[str]]) -> None:
        """Path autocomplete.

        Given a (partial) path, returns matching directory entries that start
        with the typed segment.  If the input ends in ``/``, lists all entries
        in that directory.

        Query params:
          path  the (possibly partial) input path
          kind  optional filter: "dir" (default) or "all"
          limit optional max results (default 30, max 200)
        """
        raw = (qs.get("path") or [""])[0]
        kind = (qs.get("kind") or ["dir"])[0]
        limit = int((qs.get("limit") or ["30"])[0])
        limit = max(1, min(limit, 200))

        if not raw:
            return self._json(200, {"entries": []})

        # Expand ~ and resolve
        expanded = os.path.expanduser(raw)
        # Choose parent dir + prefix to match
        if expanded.endswith("/"):
            parent_str = expanded.rstrip("/") or "/"
            prefix = ""
        else:
            parent_str = os.path.dirname(expanded) or "/"
            prefix = os.path.basename(expanded)

        try:
            parent = Path(parent_str).resolve()
        except OSError:
            return self._json(200, {"entries": []})

        # Path safety
        if not any(_is_within(parent, root) for root in ALLOWED_ROOTS):
            raise PathError(f"path not allowed: {parent}", 403)
        if _is_sensitive(parent):
            _sel_audit("access_denied", str(parent), outcome="denied")
            return self._json(200, {"entries": []})
        if not parent.exists() or not parent.is_dir():
            return self._json(200, {"entries": []})
        _sel_audit("complete", str(parent))

        results: list[dict] = []
        try:
            children = sorted(parent.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (OSError, PermissionError):
            return self._json(200, {"entries": []})

        plower = prefix.lower()
        for child in children:
            if len(results) >= limit:
                break
            name = child.name
            if name in IGNORE_DIRS:
                continue
            if name in SENSITIVE_DIRS:
                continue
            # Deny-by-default for .kirocrew root: only safe subdir names may
            # appear in completions (see _list_dir for rationale).
            if parent.name == ".kirocrew" and name not in _KIROCREW_SAFE_SUBDIRS:
                continue
            if plower and not name.lower().startswith(plower):
                continue
            is_dir = child.is_dir()
            if kind == "dir" and not is_dir:
                continue
            results.append(
                {
                    "name": name,
                    "path": str(child) + ("/" if is_dir else ""),
                    "type": "dir" if is_dir else _file_kind(child),
                }
            )
        return self._json(200, {"parent": str(parent), "prefix": prefix, "entries": results})

    # ----- helpers -----
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), FileExplorerHandler)
    logger.info(
        "listening on http://127.0.0.1:%d  rg=%s  allowed=%s",
        PORT,
        _has_rg(),
        [str(p) for p in ALLOWED_ROOTS],
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
