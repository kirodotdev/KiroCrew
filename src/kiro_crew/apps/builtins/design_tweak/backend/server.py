"""Composition root for the Design Tweak backend.

The boundary modules own durable state, preview serving, dev-server plumbing,
and HTTP dispatch.  This module owns process creation, security-policy calls,
mutable runtime state, compatibility names, and process lifecycle.
"""

from __future__ import annotations

import atexit
import glob
import html as _html
import http.client
import json
import os
import re
import selectors
import shutil
import socket
import subprocess
import sys as _sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from kiro_crew.apps.builtins.design_tweak.backend import (
    dev_preview,
    http_api,
    preview_files,
    request_state,
)
from kiro_crew.apps.proxy_auth import verify_proxy_request
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.platform_compat import (
    CREATE_NEW_PROCESS_GROUP,
    IS_POSIX,
    SIGKILL,
    SIGTERM,
    get_ppid,
    kill_process_tree,
    trusted_system_bin,
)
from kiro_crew.security import (
    DENIED_ROOT_PARTS,
    get_credential_patterns,
    is_sensitive_path,
    path_contains_sensitive,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# These names are intentionally present on the runtime module.  Boundary
# implementations resolve them late through the server-owned network,
# filesystem, clock, and platform seams.
_RUNTIME_COMPAT_IMPORTS = (
    BaseHTTPRequestHandler,
    FileTooLargeError,
    SIGKILL,
    SIGTERM,
    _html,
    cast,
    get_credential_patterns,
    get_ppid,
    glob,
    http.client,
    is_sensitive_path,
    kill_process_tree,
    parse_qs,
    path_contains_sensitive,
    safe_read_file_bytes_nolink,
    selectors,
    sel,
    shutil,
    socket,
    tempfile,
    unquote,
    urllib.error,
    urllib.request,
    urlparse,
    uuid,
    verify_proxy_request,
)


class _RuntimeFacade:
    """Globals-backed runtime for detached module execution."""

    def __getattr__(self, name: str) -> Any:
        # A module object answers a missing name with AttributeError, and the
        # facade has to match: a bare ``globals()[name]`` raises KeyError, which
        # `getattr(runtime, name, default)` does not catch, so a probe with a
        # default would propagate instead of taking the default.
        try:
            return globals()[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        globals()[name] = value


# Normal imports use the real module object, so replacing ``server.<name>`` is
# immediately visible across every boundary.  Detached execution without a
# sys.modules entry uses the facade for the same live get/set semantics.
runtime: Any
if __name__ in _sys.modules:
    runtime = _sys.modules[__name__]
else:  # pragma: no cover - detached execution is exercised separately
    runtime = _RuntimeFacade()


def _manifest_version() -> str:
    """Read the backend version from its app manifest."""

    try:
        path = Path(__file__).resolve().parent.parent / "app.json"
        return str(json.loads(path.read_text("utf-8")).get("version", "")) or "0.0.0"
    except (OSError, ValueError):
        return "0.0.0"


VERSION = _manifest_version()
PORT = int(os.environ.get("PORT", 9110))
APP_NAME = os.environ.get("KIROCREW_APP_NAME") or "design-tweak"

PROXY_PUBLIC_BASE = f"/apps/{APP_NAME}/api/proxy/"
INJECT_PUBLIC = f"/apps/{APP_NAME}/api/proxy-inject.js"
INJECT_FILE = Path(__file__).resolve().parent.parent / "inject" / "select-to-edit.js"

# Exactly one legacy source is active: a local folder or an allow-listed
# loopback dev URL.  Registered projects carry their own live preview state.
_ROOT = ""
_TARGET = ""

_DATA_ENV = os.environ.get("KIROCREW_APP_DATA_DIR") or os.environ.get("KIROCREW_APP_DATA", "")
if _DATA_ENV:
    DATA_DIR = Path(_DATA_ENV).expanduser().resolve()
else:
    _home = os.environ.get("KIROCREW_HOME")
    _base = Path(_home).expanduser() if _home else Path(os.path.expanduser("~")) / ".kiro" / "crew"
    DATA_DIR = (_base / "apps" / APP_NAME / "data").resolve()

QUEUE_DIR = DATA_DIR / "queue"
HANDLED_DIR = DATA_DIR / "handled"
CONFIG_FILE = DATA_DIR / "config.json"
# Directory creation stays in main(); imports are side-effect-free and never
# touch the operator's real data home.

# Directory trees this preview server must NEVER serve out of, whatever the user
# registered as a project root.
#
# WHY A TREE AND NOT MORE FILENAMES. `is_sensitive_path()` gates only the
# ENUMERATED LEAVES under the crew home (`security._CREW_SECRET_LEAVES`), so
# `is_sensitive_path("~/.kiro/crew")` is False and every unlisted file under it is
# servable. Registering `~` as a project is a natural pick when a site lives at
# `~/index.html`, and that alone put `~/.kiro/crew/apps/<app>/.app_secret` — the
# proxy-auth HMAC credential shared by every app backend — and
# `~/.kiro/crew/history/*.jsonl` (chat transcripts) one same-origin `fetch()` away
# from any script on the previewed page.
#
# Adding those two names to a denylist would close the two we happened to think
# of. Refusing the whole tree closes the class, including files a later release
# adds. `~/.kiro` is included because it is kiro-cli's own directory (it holds the
# auth store) and the crew home is nested inside it.
_HOME_REAL = os.path.realpath(os.path.expanduser("~"))
# The crew data home, resolved the same way `DATA_DIR` resolves it. Needed
# SEPARATELY from `DATA_DIR`: that is only this app's own subtree, so a relocated
# `KIROCREW_HOME` left the rest of the home — `history/*.jsonl` transcripts,
# `sessions.db`, the governance policy files — outside every entry below, and one
# `fetch()` from a previewed page away. The default path is already covered by the
# `~/.kiro` entry; this is what closes the custom-home case.
_CREW_HOME_ENV = os.environ.get("KIROCREW_HOME")
_CREW_HOME = (
    Path(_CREW_HOME_ENV).expanduser() if _CREW_HOME_ENV else Path(_HOME_REAL) / ".kiro" / "crew"
)
_KIROCREW_INTERNAL_DIRS: tuple[str, ...] = tuple(
    os.path.realpath(path)
    for path in (
        os.path.join(_HOME_REAL, ".kiro"),  # kiro-cli's dir; crew home nests inside
        os.path.join(_HOME_REAL, ".kirocrew"),  # pre-move legacy data home
        str(_CREW_HOME),  # a relocated crew home (KIROCREW_HOME)
        str(DATA_DIR),  # a relocated app data home (KIROCREW_APP_DATA_DIR)
    )
)

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB cap on a single payload
MAX_STATIC_BYTES = preview_files.MAX_STATIC_BYTES
# Ceiling on comments in ONE draft request. Generous for the intended use (a
# batch of visual edits is a handful, not hundreds) and low enough that a
# previewed page looping on `submitComment` cannot grow the queue file without
# bound. The draft is reviewable and Send is a separate step, so this bounds
# storage rather than gating the feature.
MAX_DRAFT_COMMENTS = 200
# Ceiling on entries in ONE thread, request-level or per comment. `/thread` is
# the agent's progress channel, so it is written far more often than a human
# comments — and every append rewrites the WHOLE record, so an unbounded thread
# costs quadratic rewrite work on top of unbounded disk. A stuck agent looping
# on progress posts is the realistic path there, not an attacker.
MAX_THREAD_ENTRIES = 500
# On-disk ceiling for ONE queue record. The SAME number gates the write and the
# read deliberately: a record the writer accepts but the reader refuses is a
# draft the user can no longer see, and silently losing queued work is worse than
# refusing the append that would have caused it. Kept distinct from
# `MAX_BODY_BYTES` (which bounds ONE inbound payload) because a record
# accumulates many payloads over its life — they are equal today, and conflating
# them would let a change to the inbound cap move this ceiling by accident.
MAX_RECORD_BYTES = 2 * 1024 * 1024

_RecordTooLarge = request_state.RecordTooLarge
_PathEscape = request_state.PathEscape
_IncompleteBody = http_api.IncompleteBody
_ID_RE = request_state.REQUEST_ID_RE
_COMMENT_STATUSES = request_state.COMMENT_STATUSES

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})
# Credential dirs a previewed "project" folder may never be, shared with
# design_critique's local-target guard via kiro_crew.security. The shared
# `is_sensitive_path()` floor covers the crew home and the governance trust
# root; these are the plain dot-dirs it does not need to know about.
_DENIED_ROOT_PARTS = DENIED_ROOT_PARTS

# One native picker may own the foreground dialog.  Queue and registry
# read-modify-write transactions share one reentrant lock; process/network work
# remains outside it.
_PICK_LOCK = threading.Lock()
_QUEUE_LOCK = threading.RLock()

_CTYPE_OVERRIDES = preview_files.CTYPE_OVERRIDES
_CTYPE_DEFAULT = preview_files.CTYPE_DEFAULT
_PROXY_CTYPES = preview_files.PROXY_CTYPES
_ENTRY_CANDIDATES = preview_files.ENTRY_CANDIDATES
_SCAN_SKIP_DIRS = preview_files.SCAN_SKIP_DIRS
_SCRIPT_TAG_RE = preview_files.SCRIPT_TAG_RE
_ATTR_TYPE_MODULE_RE = preview_files.ATTR_TYPE_MODULE_RE
_ATTR_SRC_RE = preview_files.ATTR_SRC_RE
_UNBUNDLED_EXTS = preview_files.UNBUNDLED_EXTS
_PROJECT_SECRET_NAMES = preview_files.PROJECT_SECRET_NAMES
_PROJECT_SECRET_DIRS = preview_files.PROJECT_SECRET_DIRS
_PROJECT_SECRET_SUFFIXES = preview_files.PROJECT_SECRET_SUFFIXES
_PEM_PRIVATE_KEY_RE = preview_files.PEM_PRIVATE_KEY_RE
_PEM_MARKER_RE = preview_files.PEM_MARKER_RE
_PEM_BODY_RE = preview_files.PEM_BODY_RE
# Each loaded server-module instance owns its listener handles; detached or
# reloaded instances never share them.
_STATIC_SRV: dict[str, dict[str, Any]] = {}

_NODE_BIN_DIRS = dev_preview.NODE_BIN_DIRS
_NVM_GLOB = dev_preview.NVM_GLOB
_CHILD_ENV_STRIP = dev_preview.CHILD_ENV_STRIP
_CHILD_ENV_STRIP_PREFIXES = dev_preview.CHILD_ENV_STRIP_PREFIXES
_LOCKFILES = dev_preview.LOCKFILES
_DEV_SCRIPTS = dev_preview.DEV_SCRIPTS
_PROC_TREE_MAX_DEPTH = dev_preview.PROC_TREE_MAX_DEPTH

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_LSOF_TIMEOUT = 4
_PROBE_TIMEOUT = 1.5
_START_TIMEOUT = 45
_STOP_GRACE = 3
_RELAY_TIMEOUT = 30
_WS_IDLE = 3600
_CLIENT_READ_TIMEOUT = preview_files.CLIENT_READ_TIMEOUT
_OVERLAY_PATH = preview_files.OVERLAY_PATH

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_CREDENTIAL_REQUEST_HEADERS = {"cookie", "authorization"}
_CREDENTIAL_RESPONSE_HEADERS = {"set-cookie", "set-cookie2"}

# Owned processes and adopted-server proxies share this table.  ``proc is None``
# means the listener belongs to the user and only our proxy may be stopped.
_DEV_PROCS: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Durable state compatibility surface
# ---------------------------------------------------------------------------


def _is_kirocrew_internal(target: Path) -> bool:
    return request_state.is_kirocrew_internal(runtime, target)


def _contained(base: Path | str, candidate: str = "") -> Path:
    return request_state.contain_path(base, candidate)


def _request_file(base: Path, rid: str) -> Path:
    return request_state.request_file(runtime, base, rid)


def _atomic_write_json(
    path: Path,
    payload: dict,
    *,
    max_bytes: int | None = None,
) -> None:
    return request_state.atomic_write_json(runtime, path, payload, max_bytes=max_bytes)


def _load_cfg() -> dict:
    return request_state.load_config(runtime)


def _save_cfg(cfg: dict) -> None:
    return request_state.save_config(runtime, cfg)


_CFG = _load_cfg()


def _active_project() -> dict | None:
    return request_state.active_project(runtime)


def _next_number(project_id: str = "") -> int:
    return request_state.next_request_number(runtime, project_id)


def _new_id() -> str:
    return request_state.new_request_id()


def _now_iso() -> str:
    return request_state.utc_now_iso()


def _pending_files() -> list[Path]:
    return request_state.pending_files(runtime)


def _el_name(el: dict) -> str:
    return request_state.element_name(el)


def _request_status(req: dict) -> str:
    return request_state.request_status(req)


def _is_draft(req: dict) -> bool:
    return request_state.is_draft(runtime, req)


def _redact_text(value: object) -> str:
    """Apply the redaction floor to one string, URLs before credentials.

    The order is load-bearing: `redact_exfiltration_urls` keys off the URL's
    host, so a credential-first pass would rewrite a token inside a URL to
    `[REDACTED: credential]` and leave the exfiltration host itself standing.
    That is why both edges route through this one function rather than inlining
    the pair — a second copy of the order is how the two edges drift.
    """

    text, _ = redact_exfiltration_urls(str(value or ""))
    text, _ = redact_credentials(text)
    return text


def _redact_incoming_thread_text(value: object) -> str:
    """Redact agent-authored thread text before it reaches durable storage.

    Named separately from `_redact_text` because it guards the opposite edge:
    the panel renders this text later, so a leaked credential must not exist on
    disk in the first place. It is applied per-field, not to the whole payload —
    `role` and `status` are allow-listed to fixed vocabularies and `ts` is
    generated server-side, so none of them can persist an arbitrary string.
    """

    return _redact_text(value)


def _redact_thread(thread: object) -> list[dict]:
    return request_state.redact_thread(runtime, thread)


def _summarize_comment(comment: dict) -> dict:
    return request_state.summarize_comment(runtime, comment)


def _summarize(request: dict) -> dict:
    return request_state.summarize_request(runtime, request)


def _proj_for_preview(preview_url: str) -> tuple[dict | None, str]:
    return request_state.project_for_preview(runtime, preview_url)


def _project_by_id(project_id: str) -> dict | None:
    return request_state.project_by_id(runtime, project_id)


def _resolve_project(payload: dict) -> tuple[str, str, str]:
    return request_state.resolve_project(runtime, payload)


def _contained_source(root: str, rel: str) -> str:
    return request_state.contained_source(runtime, root, rel)


def _sanitize_selection_sources(sel_obj: Any, root: str) -> None:
    return request_state.sanitize_selection_sources(runtime, sel_obj, root)


def _read_request(fp: Path) -> dict | None:
    """Read only after the MAX_RECORD_BYTES st_size gate, before read_text."""

    return request_state.read_request(runtime, fp)


def _write_request(fp: Path, req: dict) -> None:
    """Publish through the shared chokepoint with max_bytes=MAX_RECORD_BYTES."""

    return request_state.write_request(runtime, fp, req)


def _find_request(rid: str) -> Path | None:
    return request_state.find_request(runtime, rid)


def _open_draft_file(project_id: str) -> Path | None:
    return request_state.open_draft_file(runtime, project_id)


def _valid_target(url: str) -> bool:
    return request_state.valid_target(runtime, url)


def _valid_root(path: str) -> Path | None:
    return request_state.valid_root(runtime, path)


# Resume the active, still-readable project without creating any directory.
_boot_active = _active_project()
if _boot_active:
    _boot_root = _valid_root(_boot_active.get("path", ""))
    if _boot_root is not None:
        _ROOT = str(_boot_root)


# ---------------------------------------------------------------------------
# File preview compatibility surface
# ---------------------------------------------------------------------------


def _guess_ctype(p: Path) -> str:
    return preview_files.guess_ctype(runtime, p)


def _safe_upstream_ctype(raw: str | None, path: str) -> str:
    return preview_files.safe_upstream_ctype(runtime, raw, path)


def _header_value(value: str) -> str:
    """Strip controls and bound a value before a response-header sink."""

    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n")
    return cleaned[:256]


def _unbundled_entry(html: bytes) -> str:
    return preview_files.unbundled_entry(runtime, html)


def _find_entry(folder: Path, root: Path | None = None) -> Path | None:
    return preview_files.find_entry(runtime, folder, root)


def _scan_html(root: Path, limit: int = 40, max_depth: int = 3) -> list[str]:
    return preview_files.scan_html(runtime, root, limit, max_depth)


def _rewrite_html(
    body: bytes,
    base: str | None = PROXY_PUBLIC_BASE,
    script: str = INJECT_PUBLIC,
) -> bytes:
    return preview_files.rewrite_html(runtime, body, base, script)


def _needs_dev_server_body(root: Path, page: Path, entry: str) -> bytes:
    return preview_files.needs_dev_server_body(runtime, root, page, entry)


def _no_entry_response(
    root: Path,
    folder: Path,
    base: str,
    missing: str = "",
) -> tuple[int, str, bytes]:
    return preview_files.no_entry_response(runtime, root, folder, base, missing)


def _contains_credential(data: bytes) -> bool:
    return preview_files.contains_credential(runtime, data)


def _is_project_secret(root: Path, target: Path) -> bool:
    return preview_files.is_project_secret(runtime, root, target)


def _static_response(
    root_str: str,
    rel: str,
    base: str,
    script: str = INJECT_PUBLIC,
) -> tuple[int, str, bytes]:
    """Serve only after containment and the _is_kirocrew_internal policy floor."""

    return preview_files.static_response(runtime, root_str, rel, base, script)


def _static_preview_base(project_id: str) -> str:
    return preview_files.static_preview_base(runtime, project_id)


def _stop_static_preview(project_id: str) -> None:
    return preview_files.stop_static_preview(runtime, project_id)


def _static_preview_url(project_id: str) -> str:
    return preview_files.static_preview_url(runtime, project_id)


class _StaticInjectHandler(preview_files.StaticInjectHandlerBase):
    runtime = runtime
    timeout = _CLIENT_READ_TIMEOUT


# ---------------------------------------------------------------------------
# Dev-server discovery, lifecycle, and proxy compatibility surface
# ---------------------------------------------------------------------------


def _lsof_fields(args: list[str]) -> list[dict]:
    """Run the trusted system lsof binary and parse its field records."""

    lsof = trusted_system_bin("lsof")
    if not lsof:
        return []
    try:
        result = subprocess.run(
            [lsof, *args],
            capture_output=True,
            timeout=_LSOF_TIMEOUT,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[dict] = []
    pid = ""
    for line in result.stdout.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid = value
        elif tag == "n":
            out.append({"pid": pid, "name": value})
    return out


def _loopback_listeners() -> dict[int, int]:
    return dev_preview.loopback_listeners(runtime)


def _cwd_for_pids(pids: list[int]) -> dict[int, str]:
    return dev_preview.cwd_for_pids(runtime, pids)


def _serves_html(port: int) -> bool:
    return dev_preview.serves_html(runtime, port)


def _detect_dev_servers(root: Path, probe: bool = True) -> list[dict]:
    return dev_preview.detect_dev_servers(runtime, root, probe)


def _auto_dev_server(root: Path) -> str:
    return dev_preview.auto_dev_server(runtime, root)


def _node_bin_dirs() -> list[Path]:
    return dev_preview.node_bin_dirs(runtime)


def _resolve_bin(name: str) -> Path | None:
    return dev_preview.resolve_bin(runtime, name)


def _child_env(bin_dir: Path) -> dict:
    return dev_preview.child_env(runtime, bin_dir)


def _pkg_scripts(root: Path) -> dict:
    return dev_preview.pkg_scripts(runtime, root)


def _dev_command(root: Path) -> list[str]:
    return dev_preview.dev_command(runtime, root)


def _classify_project(root: Path) -> dict:
    return dev_preview.classify_project(runtime, root)


def _stop_inject_proxy(rec: dict) -> None:
    return dev_preview.stop_inject_proxy(runtime, rec)


def _start_inject_proxy(dev_url: str) -> tuple[object | None, str]:
    return dev_preview.start_inject_proxy(runtime, dev_url)


def _front_with_proxy(project_id: str, dev_url: str) -> str:
    # The boundary's bind-failure result is `return ""`; never expose upstream.
    return dev_preview.front_with_proxy(runtime, project_id, dev_url)


def _stop_dev_proc(project_id: str) -> bool:
    return dev_preview.stop_dev_proc(runtime, project_id)


def _stop_all_dev_procs() -> None:
    return dev_preview.stop_all_dev_procs(runtime)


def _dev_proc_alive(project_id: str) -> bool:
    return dev_preview.dev_proc_alive(runtime, project_id)


def _in_proc_tree(pid: int, root_pid: int, pgid: int | None) -> bool:
    return dev_preview.in_proc_tree(runtime, pid, root_pid, pgid)


class _DevProxyHandler(dev_preview.DevProxyHandlerBase):
    runtime = runtime
    timeout = _CLIENT_READ_TIMEOUT


def _start_dev_proc(project_id: str, root: Path) -> dict:
    """Start an owned project dev server and discover the port it selected."""

    if _dev_proc_alive(project_id):
        rec = _DEV_PROCS[project_id]
        return {
            "ok": True,
            "url": rec.get("proxyUrl") or rec["url"],
            "devUrl": rec["url"],
            "already": True,
        }
    _stop_dev_proc(project_id)

    cmd = _dev_command(root)
    if not cmd:
        return {
            "ok": False,
            "error": "No dev script found in package.json (looked for: "
            + ", ".join(_DEV_SCRIPTS)
            + ").",
        }

    # Resolve the executable before spawn; the backend deliberately does not
    # trust an agent-writable PATH for this process boundary.
    binary = _resolve_bin(cmd[0])
    if binary is None:
        looked = ", ".join(str(directory) for directory in _node_bin_dirs()[:4])
        return {
            "ok": False,
            "error": f"Could not find `{cmd[0]}`. Design Tweak's backend does not inherit "
            f"your shell's PATH, and {cmd[0]} is not in the usual places "
            f"({looked}…). Start the dev server yourself, then press "
            f"Dev server to connect to it.",
        }

    if not (root / "node_modules").is_dir():
        return {
            "ok": False,
            "error": f"node_modules is missing — run `{cmd[0]} install` in {root.name} first.",
        }

    try:
        log = _contained(DATA_DIR, f"devserver-{project_id}.log")
    except _PathEscape:
        return {"ok": False, "error": f"invalid project id: {project_id!r}"}
    try:
        handle = log.open("wb")
        proc = subprocess.Popen(  # noqa: S603 (user's own project)
            [str(binary), *cmd[1:]],
            cwd=str(root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=_child_env(binary.parent),
            # npm-style launchers fork the listener, so each owned process gets a
            # platform-appropriate group/tree that cleanup can terminate.
            start_new_session=IS_POSIX,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"could not start `{' '.join(cmd)}`: {exc}"}

    pgid = None
    if IS_POSIX:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
    _DEV_PROCS[project_id] = {
        "proc": proc,
        "pgid": pgid,
        "url": "",
        "log": str(log),
        "proxy": None,
        "proxyUrl": "",
        "proxyFor": "",
    }

    deadline = time.time() + _START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log.read_text("utf-8", errors="replace")[-800:]
            except OSError:
                pass
            _DEV_PROCS.pop(project_id, None)
            return {
                "ok": False,
                "error": f"`{' '.join(cmd)}` exited ({proc.returncode}).",
                "log": tail,
            }
        for candidate in _detect_dev_servers(root, probe=False):
            if candidate["pid"] == proc.pid or _in_proc_tree(candidate["pid"], proc.pid, pgid):
                _DEV_PROCS[project_id]["url"] = candidate["url"]
                framed = _front_with_proxy(project_id, candidate["url"])
                return {
                    "ok": True,
                    "url": framed,
                    "devUrl": candidate["url"],
                    "port": candidate["port"],
                    "injected": bool(framed) and framed != candidate["url"],
                }
        time.sleep(0.4)

    _stop_dev_proc(project_id)
    return {
        "ok": False,
        "error": f"`{' '.join(cmd)}` did not start listening within {_START_TIMEOUT}s.",
    }


class Handler(http_api.Handler):
    runtime = runtime

    def _h_pick_folder(self) -> None:
        """Open the trusted macOS native folder picker."""

        if _sys.platform != "darwin":
            return self._json(501, {"error": "native picker is macOS-only"})
        if not _PICK_LOCK.acquire(blocking=False):
            return self._json(409, {"error": "a folder picker is already open"})
        try:
            script = (
                'tell application "System Events" to activate\n'
                'POSIX path of (choose folder with prompt "Select a web app folder for Design Tweak")'
            )
            osascript = trusted_system_bin("osascript")
            if not osascript:
                return self._json(
                    501,
                    {
                        "error": "native picker is unavailable",
                        "code": "picker_unavailable",
                    },
                )
            result = subprocess.run(
                [osascript, "-e", script],
                capture_output=True,
                timeout=180,
                **UTF8_TEXT,
            )
        except subprocess.TimeoutExpired:
            return self._json(408, {"error": "picker timed out"})
        except OSError as exc:
            return self._json(500, {"error": str(exc)})
        finally:
            _PICK_LOCK.release()
        if result.returncode != 0:
            error = (result.stderr or "").strip()
            if "-128" in error or "canceled" in error.lower():
                return self._json(200, {"ok": False, "canceled": True})
            return self._json(500, {"error": error[-200:] or "picker failed"})
        path = result.stdout.strip().rstrip("/")
        if not path:
            return self._json(200, {"ok": False, "canceled": True})
        return self._json(200, {"ok": True, "path": path})


atexit.register(_stop_all_dev_procs)


def main() -> int:
    """Create data directories and run the loopback HTTP server."""

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    HANDLED_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[{APP_NAME}] listening on http://127.0.0.1:{PORT}  data={DATA_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
