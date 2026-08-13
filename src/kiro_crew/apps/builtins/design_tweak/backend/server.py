"""Select-to-Edit backend for Kiro Crew.

A small stdlib-only HTTP server that receives `visual_edit_request` payloads
from the dashboard app page and persists them to a queue directory that the
`visual-edit` skill instructs the agent to read.

Bound to localhost; Kiro Crew proxies `/apps/design-tweak/api/*` to
this process (stripping the prefix, so the server sees `/api/<path>` or `/<path>`).

Every proxied request carries the gateway's `X-KiroCrew-Proxy` HMAC, verified
before dispatch (see `kiro_crew.apps.proxy_auth`); only `/health` is left
unauthenticated because the gateway's liveness probe hits the backend directly.

Endpoints (as seen after proxy prefix strip):
  GET  /health              → {"status","app","version","pending"}
  POST /submit              → body = visual_edit_request; persists → {"ok","id","savedTo"}
  GET  /queue               → {"pending":[{id,createdAt,comment,mode,count,previewUrl}]}
  GET  /latest              → newest pending request (full payload) or {}
  POST /clear?id=<id>       → move request to handled/ → {"ok","id"}
  POST /delivered?id=<id>   → ack that a sealed request's prompt reached the agent
  POST /thread?id=<id>      → append {role,text,status?} to a request thread

Delivery model: this process cannot call the agent directly (separate process).
Instead it writes the structured payload to the app data queue dir; the bundled
`visual-edit` skill teaches the agent to read + act on it. This is the spec's
sanctioned "well-known file the agent watches" fallback.
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

from kiro_crew.apps.proxy_auth import verify_proxy_request
from kiro_crew.hooks import safe_read_file_bytes_nolink
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
    get_credential_patterns,
    is_sensitive_path,
    path_contains_sensitive,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel


def _manifest_version() -> str:
    """Read the version from app.json rather than duplicating it here.

    A hardcoded constant drifts silently — this one sat at 0.6.0 while the
    manifest said 0.7.1, so /health reported a version that had not been real for
    two releases, which is worse than reporting nothing.
    """
    try:
        p = Path(__file__).resolve().parent.parent / "app.json"
        return str(json.loads(p.read_text("utf-8")).get("version", "")) or "0.0.0"
    except (OSError, ValueError):
        return "0.0.0"


VERSION = _manifest_version()
PORT = int(os.environ.get("PORT", 9110))
APP_NAME = os.environ.get("KIROCREW_APP_NAME") or "design-tweak"

# Public (browser-facing) proxy paths — resolved through the gateway proxy.
PROXY_PUBLIC_BASE = f"/apps/{APP_NAME}/api/proxy/"
INJECT_PUBLIC = f"/apps/{APP_NAME}/api/proxy-inject.js"
# The drop-in overlay lives at <app>/inject/select-to-edit.js
INJECT_FILE = Path(__file__).resolve().parent.parent / "inject" / "select-to-edit.js"

# Source of the previewed app. Exactly one of these is active at a time:
#   _ROOT   — an absolute folder path served directly by this backend (preferred).
#   _TARGET — a localhost dev-server URL reverse-proxied (for Vite/HMR projects).
# Both are localhost/local-filesystem only (no SSRF, no arbitrary FS escape).
_ROOT = ""
_TARGET = ""

# Resolve the app data dir. The host injects KIROCREW_APP_DATA_DIR for builtin
# app backends; fall back to the platform-standard location under
# $KIROCREW_HOME/apps/<name>/data (KIROCREW_HOME defaults to ~/.kiro/crew, the
# data home nested under kiro-cli's ~/.kiro/ — NOT the pre-move ~/.kirocrew).
_DATA_ENV = os.environ.get("KIROCREW_APP_DATA_DIR") or os.environ.get("KIROCREW_APP_DATA") or ""
if _DATA_ENV:
    DATA_DIR = Path(_DATA_ENV).expanduser().resolve()
else:
    _home = os.environ.get("KIROCREW_HOME")
    _base = (
        Path(_home).expanduser() if _home else (Path(os.path.expanduser("~")) / ".kiro" / "crew")
    )
    DATA_DIR = (_base / "apps" / APP_NAME / "data").resolve()

QUEUE_DIR = DATA_DIR / "queue"
HANDLED_DIR = DATA_DIR / "handled"
# Deliberately NOT created here: this runs at IMPORT time, before a test's
# isolation fixture (`isolated_queue`) has a chance to monkeypatch these paths
# to a private tmp tree, so importing the module for any reason -- including
# bare pytest collection -- created real directories under the operator's own
# $KIROCREW_HOME. Creation is deferred to `main()`, the actual process entry
# point, which is never reached by an import alone.

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
    os.path.realpath(p)
    for p in (
        os.path.join(_HOME_REAL, ".kiro"),  # kiro-cli's dir; crew home nests inside
        os.path.join(_HOME_REAL, ".kirocrew"),  # pre-move legacy data home
        str(_CREW_HOME),  # a relocated crew home (KIROCREW_HOME)
        str(DATA_DIR),  # a relocated app data home (KIROCREW_APP_DATA_DIR)
    )
)


def _is_kirocrew_internal(target: Path) -> bool:
    """Whether *target* resolves inside one of Kiro Crew's own directory trees.

    Separator-aware so a sibling like `~/.kiro-backup` is not caught by a bare
    prefix test, matching `_contained`'s reasoning.
    """
    real = os.path.realpath(target)
    for base in _KIROCREW_INTERNAL_DIRS:
        if real == base or real.startswith(base + os.sep):
            return True
    return False


MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB cap on a single payload
# Ceiling on ONE statically-served preview file. Generous for real web assets
# (bundles, fonts, images) while bounding the buffer: `_static_response` reads the
# whole file into memory, so without this a large asset sitting in the previewed
# project is enough to exhaust the backend. Distinct from MAX_BODY_BYTES, which
# caps an inbound request body rather than an outbound file.
MAX_STATIC_BYTES = 64 * 1024 * 1024  # 64 MB
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


class _RecordTooLarge(ValueError):
    """A queue record would serialize past what its reader accepts."""


_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")  # queue file id safety

# The only hosts this backend will ever fetch from (dev-server reverse proxy).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})
# Characters left unescaped when re-composing a proxied request path. "%" is
# safe so an already-percent-encoded path is not double-encoded; CR/LF are
# rejected outright before this point.
_PROXY_PATH_SAFE = "%/@:~!$&()*+,;="
# Credential dirs a previewed "project" folder may never be. The shared
# `is_sensitive_path()` floor covers the crew home and the governance trust
# root; these are the plain dot-dirs it does not need to know about.
_DENIED_ROOT_PARTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})

_PICK_LOCK = threading.Lock()  # one native folder picker at a time

# Serializes every read-modify-write transaction against the queue on disk.
#
# WHY THIS EXISTS. The backend is a `ThreadingHTTPServer`, so two dashboard tabs
# submitting at the same moment are two handler threads. Without this lock they
# each `_read_request` the same draft, append their own comment to the copy they
# read, and `_write_request` it back -- last writer wins and the other comment is
# silently gone. The queue file is the whole record, so that is data loss, not a
# stale read. The same shape applies to every other queue mutation (seal-on-send,
# delete-comment, /thread append, clear, delete).
#
# Held across READ THROUGH WRITE as ONE critical section -- locking only the write
# is the identical bug, because the lost update happens in the gap between the
# read and the write.
#
# An RLock, not a Lock, for two reasons:
#   1. Re-entrancy. A queue transaction legitimately nests helpers that are
#      themselves whole-queue operations (`_h_submit` needs `_open_draft_file`
#      AND `_next_number`, both of which scan every request file). RLock lets a
#      helper take the lock defensively without deadlocking a caller that
#      already holds it, so a future edit cannot introduce a silent self-
#      deadlock.
#   2. Deadlock-freedom is structural, not conventional. Deadlock needs either a
#      non-reentrant self-acquire (RLock rules that out) or two locks taken in
#      inconsistent order. There is exactly ONE lock on this path, and the only
#      other lock in the module (`_PICK_LOCK`) is taken solely by
#      `_h_pick_folder`, which never touches the queue -- so no thread ever holds
#      one while wanting the other and no cycle can form.
#
# Critical sections stay tight: each transaction computes its `(code, payload)`
# and the HTTP response is written AFTER the lock is released, so a client that
# stalls reading its socket can never pin the queue. No subprocess, no network
# I/O, and no sleeping inside.
_QUEUE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Path containment. THE single barrier every filesystem operation below goes
# through — see `_contained`.
# ---------------------------------------------------------------------------
class _PathEscape(ValueError):
    """A joined path landed outside the base directory it had to stay inside."""


def _contained(base: Path | str, candidate: str = "") -> Path:
    """Join *candidate* under *base* and return it only if it stays inside.

    This is the ONE path sanitizer in this module. It normalises **both** sides
    with ``os.path.realpath`` — which collapses ``..`` segments and resolves
    symlinks — and then requires the result to be *base* itself or a descendant
    of it. Anything else raises `_PathEscape`, so the RETURN VALUE is always
    contained; callers must use the returned path and never the one they passed
    in. An absolute *candidate* is handled too: ``os.path.join`` lets it win,
    and the containment check then rejects it.

    Both checks are load-bearing:

    * ``cand.startswith(base_real)`` on the normalised candidate is the
      normalise-then-check shape that static analysis models as a containment
      barrier. The equally strong ``Path(...).resolve()`` + ``relative_to()``
      form this replaced is *not* modelled, which is why every read/write/stat
      downstream of it showed up as a `py/path-injection` finding.
    * the separator test then closes the sibling-prefix hole a bare prefix
      check leaves open: ``/srv/app-evil`` starts with ``/srv/app`` but is not
      inside it.
    """
    base_real = os.path.realpath(base)
    cand = os.path.realpath(os.path.join(base_real, candidate))
    if not cand.startswith(base_real):
        raise _PathEscape(f"{cand!r} is outside {base_real!r}")
    if cand != base_real and not cand[len(base_real) :].startswith(os.sep):
        raise _PathEscape(f"{cand!r} is a sibling of {base_real!r}, not inside it")
    return Path(cand)


def _request_file(base: Path, rid: str) -> Path:
    """`<base>/<rid>.json`, contained. Raises `_PathEscape` on an unusable id.

    Two barriers, not one: `_ID_RE` rejects anything but an id-shaped token, and
    `_contained` then proves the join stayed in *base* even if that pattern is
    ever loosened.
    """
    if not rid or not _ID_RE.match(rid):
        raise _PathEscape(f"invalid request id: {rid!r}")
    return _contained(base, f"{rid}.json")


# ---------------------------------------------------------------------------
# Persistent config: registered projects, active project, request counter.
# ---------------------------------------------------------------------------
CONFIG_FILE = DATA_DIR / "config.json"


def _load_cfg() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
        if isinstance(cfg, dict):
            cfg.setdefault("projects", [])
            cfg.setdefault("activeId", "")
            cfg.setdefault("counter", 0)
            return cfg
    except (OSError, ValueError):
        pass
    return {"projects": [], "activeId": "", "counter": 0}


def _save_cfg(cfg: dict) -> None:
    # Atomic for the same reason as the queue files: a truncated config would
    # lose every registered project.
    _atomic_write_json(CONFIG_FILE, cfg)


_CFG = _load_cfg()


def _active_project() -> dict | None:
    for p in _CFG["projects"]:
        if p["id"] == _CFG["activeId"]:
            return p
    return None


def _next_number(project_id: str = "") -> int:
    """The next request number **within one project**.

    Numbering is per-app so each web app reads as its own sequence: app B's first
    request is "Request 1", not "Request 7" because six unrelated requests were
    filed against app A. Derived by scanning, not stored — a stored per-project
    counter would be a second source of truth to keep in sync, and the request
    files already know their own numbers.

    Falls back to the legacy global counter when there is no project id, so
    pre-0.9 requests and any unscoped caller keep working.
    """
    if not project_id:
        _CFG["counter"] = int(_CFG.get("counter", 0)) + 1
        _save_cfg(_CFG)
        return _CFG["counter"]
    highest = 0
    for d in (QUEUE_DIR, HANDLED_DIR):
        for fp in d.glob("*.json"):
            req = _read_request(fp)
            if not req or req.get("projectId") != project_id:
                continue
            try:
                highest = max(highest, int(req.get("number") or 0))
            except (TypeError, ValueError):
                continue
    return highest + 1


def _new_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pending_files() -> list[Path]:
    return sorted(QUEUE_DIR.glob("*.json"))


def _el_name(el: dict) -> str:
    """Human-readable element label, e.g. 'nav#site-header' or 'div.card.grid'.

    Total by construction: every field is coerced rather than trusted. This runs
    on the READ side, summarising a record that is already on disk, so it has to
    survive input the write side never sanctioned -- a queue file written by an
    older build, or hand-edited. A `TypeError` here is not a bad label, it is a
    permanently 500-ing `/queue`: the poll cannot skip the offending record, so
    the whole request list stays unreadable until someone deletes the file by
    hand. `_h_submit` rejects these shapes at the boundary now, and that guard is
    the one that gives a caller a real error; this is the containment that keeps
    one bad record from taking the list down with it.
    """
    name = str(el.get("tag") or "")
    el_id = el.get("id")
    classes = el.get("classes")
    if el_id:
        name += f"#{el_id}"
    elif isinstance(classes, (list, tuple)):
        # Only string entries can be joined; a non-string would raise, and a
        # coerced one would print as `.42`, so drop them instead.
        names = [c for c in classes[:2] if isinstance(c, str)]
        if names:
            name += "." + ".".join(names)
    return name


# ---------------------------------------------------------------------------
# Request / comment model.
#
# A queue file is ONE REQUEST that contains MANY COMMENTS as sub-items:
#
#   { type, id, number, state: "draft"|"sent", projectId, projectRoot,
#     createdAt, sentAt, thread: [...],                     # request-level notes
#     comments: [ { cid, index, status, comment, createdAt,
#                   selection, previewUrl, sourceFile,
#                   followUpTo, thread: [...] } ] }
#
# Lifecycle (seal-on-send): comments land in the project's single OPEN DRAFT.
# Sending seals that draft (state -> "sent") and it never accepts comments
# again, so the next comment always opens a fresh draft — even while the
# previous batch is still being worked.
# ---------------------------------------------------------------------------
_COMMENT_STATUSES = ("new", "sent", "done")


def _request_status(req: dict) -> str:
    """Roll a request's comment statuses up into one request-level status.

    Comment statuses are AUTHORITATIVE; `state` is only a hint about whether the
    batch was formally sealed. This asymmetry matters: an agent that writes an
    unexpected `state` (or an old file with none) must never make a request that
    is plainly in flight read as an unsent draft. So "draft" is returned only
    when nothing has happened yet — explicit draft state AND every comment new.
    """
    comments = req.get("comments") or []
    if not comments:
        return "draft"
    if all(c.get("status") == "done" for c in comments):
        return "done"
    if req.get("state") != "draft" or any(c.get("status") != "new" for c in comments):
        return "sent"
    return "draft"


def _is_draft(req: dict) -> bool:
    """True only for a request that is still open for new comments."""
    return _request_status(req) == "draft"


def _redact_text(value: object) -> str:
    """Redact one user-visible string on the way OUT.

    URLs before credentials, matching the ingest call site and every other one:
    `redact_exfiltration_urls` keys off the host, so a credential-first pass would
    rewrite the token inside a URL and leave the exfiltration host standing.

    The order is the whole reason this is a function rather than two inline calls
    at each site -- it was already duplicated, and a second copy is how the two
    edges drift.
    """
    text, _ = redact_exfiltration_urls(str(value or ""))
    text, _ = redact_credentials(text)
    return text


def _redact_thread(thread: object) -> list[dict]:
    """Redact thread text on the way OUT, as a floor under the ingest pass.

    `_h_thread` already redacts before writing, so nothing new should reach disk
    unredacted — but ingest is not the only writer. The delivery model hands the
    agent the queue JSON directly (that is how a request reaches it), so an entry
    written into the file rather than posted to `/thread`, or one persisted before
    the ingest pass existed, would otherwise be rendered verbatim in the panel.
    Redaction is an always-on floor in this repo, so it belongs on both edges.

    The same reasoning covers the comment TEXT, which reaches the panel through
    `_summarize_comment` -- see `_redact_text`.
    """
    if not isinstance(thread, list):
        return []
    out: list[dict] = []
    for entry in thread:
        if not isinstance(entry, dict):
            continue  # tolerate a malformed file rather than 500 the read path
        out.append({**entry, "text": _redact_text(entry.get("text", ""))})
    return out


def _summarize_comment(c: dict) -> dict:
    sel = c.get("selection") or {}
    # Keep only dict elements. `_h_submit` rejects a malformed selection at the
    # boundary, but a queue file written before that guard existed would still
    # crash `_el_name` here — and a 500 from this read path is unrecoverable
    # without deleting the file by hand, so the read stays tolerant.
    raw_elements = sel.get("elements") or []
    elements = [e for e in raw_elements if isinstance(e, dict)]
    el = elements[0] if elements else {}
    return {
        "cid": c.get("cid", ""),
        "index": c.get("index", 0),
        "status": c.get("status", "new"),
        # Redacted on the way OUT for the same reason as `thread` below: the
        # delivery model hands the agent the queue JSON directly, so a comment
        # rewritten in the FILE (rather than posted through `/submit`, which
        # redacts on ingest) would otherwise render verbatim in the panel.
        "comment": _redact_text(c.get("comment", "")),
        "createdAt": c.get("createdAt", ""),
        "element": _el_name(el),
        "locator": el.get("locator", ""),
        # Fallback anchors, so a pin survives its element being deleted (parent) or
        # not existing yet (point). The overlay tries them in that order.
        "parentLocator": el.get("parentLocator", ""),
        "point": el.get("point") or {},
        "count": len(elements),
        "mode": sel.get("mode", "single"),
        "previewUrl": c.get("previewUrl", ""),
        "projectId": c.get("projectId", ""),
        "sourceFile": c.get("sourceFile", ""),
        # Where the agent should edit, and how much to trust it:
        # data-kiro-source → "high", React Fiber → "medium", neither → "low".
        # Independent of how the page was served, so a dev-server project gets
        # BETTER targeting than a proxied static folder, not worse.
        "source": el.get("source") or {},
        "followUpTo": c.get("followUpTo", ""),
        "thread": _redact_thread(c.get("thread")),
    }


def _summarize(req: dict) -> dict:
    """Panel-facing shape of a request: metadata + its comments as sub-items."""
    comments = req.get("comments") or []
    return {
        "id": req.get("id", ""),
        "number": req.get("number", 0),
        "state": req.get("state", "draft"),
        "status": _request_status(req),
        "createdAt": req.get("createdAt", ""),
        "sentAt": req.get("sentAt", ""),
        # Set only once the panel confirms the prompt actually reached the agent.
        # Sealed-but-not-acknowledged is the stranded state the retry bar exists
        # for — see `_h_delivered`.
        "deliveredAt": req.get("deliveredAt", ""),
        "projectId": req.get("projectId", ""),
        "projectRoot": req.get("projectRoot", ""),
        "thread": _redact_thread(req.get("thread")),
        "doneCount": sum(1 for c in comments if c.get("status") == "done"),
        "comments": [_summarize_comment(c) for c in comments],
    }


def _proj_for_preview(preview_url: str) -> tuple[dict | None, str]:
    """Resolve (project, path-within-project) from a preview URL.

    Static previews are served from a PER-PROJECT loopback origin as
    `<project's own static base><projectId>/<rel>` (see `_static_preview_base`
    — each project gets its own ephemeral port so two projects never share a
    browser-storage origin), so the project id and the served file both fall
    out of the path. Returns `(None, "")` for any URL that isn't ours —
    notably a dev-server URL like `http://localhost:5173/pricing`, where the path
    is a ROUTE, not a file. Use `_resolve_project` instead of calling this
    directly: identity should come from an explicit id, with this as the fallback
    for older payloads.

    The static origin is matched as an EXACT prefix against each project's OWN
    recorded base — never by pattern-matching `/<seg>/<rest>` on some single
    shared origin, because a dev-server URL has the identical shape and only
    the origin distinguishes them, and there is no longer one shared origin to
    compare against. Only ALREADY-RUNNING per-project servers are checked
    (matching one never starts a new listener): a URL naming a project whose
    listener has since stopped falls through to the legacy `/api/proxy/`
    marker check below, exactly as an unrecognized URL always has.
    """
    url = str(preview_url)
    rel = ""
    matched = False
    for pid, rec in _STATIC_SRV.items():
        base = rec.get("url") or ""
        if base and url.startswith(f"{base}{pid}/"):
            rel = url[len(base) :]
            matched = True
            break
    if not matched:
        # The gateway-proxied `/apps/<app>/api/proxy/<id>/<rel>` route is gone, but
        # comments captured before its removal still carry those URLs on disk.
        marker = "/api/proxy/"
        i = url.find(marker)
        if i == -1:
            return None, ""
        rel = url[i + len(marker) :]
    rel = rel.split("?")[0].split("#")[0]
    seg = rel.split("/", 1)[0] if rel else ""
    rest = rel.split("/", 1)[1] if "/" in rel else ""
    proj = next((p for p in _CFG["projects"] if p["id"] == seg), None)
    return proj, rest


def _project_by_id(project_id: str) -> dict | None:
    if not project_id:
        return None
    return next((p for p in _CFG["projects"] if p["id"] == project_id), None)


def _resolve_project(payload: dict) -> tuple[str, str, str]:
    """Identify the project a captured comment belongs to.

    Returns `(projectId, projectRoot, sourceFile)`.

    Identity comes from an EXPLICIT `projectId` on the payload — the panel knows
    which project it is previewing, so it says so. The URL is only parsed as a
    fallback for payloads written before that field existed. This matters beyond
    tidiness: pattern-matching `/api/proxy/<id>/` only works for content this
    backend proxies, so a project previewed straight from its dev server would
    otherwise resolve to no project at all — losing its pins, its per-comment
    threads, and its grouping.

    `sourceFile` is only meaningful when the URL names a served FILE. A
    dev-server route (`/pricing`) is not a path on disk, so it is left empty and
    the agent relies on the per-element `source` block (`data-kiro-source` →
    high confidence, React Fiber → medium) instead.
    """
    preview_url = str(payload.get("previewUrl", ""))
    explicit = str(payload.get("projectId", "") or "")

    proj = _project_by_id(explicit)
    served_rel = ""
    if proj is not None:
        # Trust the id; still read the served path off the URL when it IS ours,
        # so proxied projects keep exact per-page source files.
        url_proj, served_rel = _proj_for_preview(preview_url)
        if url_proj is not None and url_proj["id"] != proj["id"]:
            served_rel = ""  # URL disagrees with the id — don't guess a file
    else:
        proj, served_rel = _proj_for_preview(preview_url)

    if proj is not None:
        root = proj["path"]
        return proj["id"], root, _contained_source(root, served_rel)
    if _ROOT:
        return explicit, _ROOT, _contained_source(_ROOT, served_rel)
    return explicit, "", ""


def _contained_source(root: str, rel: str) -> str:
    """`<root>/<rel>` as a string, or `""` if it would escape *root*.

    `rel` is PREVIEW-CONTROLLED: it is sliced out of the `previewUrl` the page
    reports, so it can carry `..` segments. Joining it to *root* unchecked
    produced a `sourceFile` that the visual-edit prompt then hands the agent as
    "the exact source file to edit" — pointing it at a path outside the project
    the user actually registered. Nothing in this backend OPENS the value, so
    the barrier protects the agent's edit target rather than a read here.

    Empty in, empty out: no `rel` means the URL named a route, not a file, and
    the agent falls back to the per-element `source` block.
    """
    if not rel:
        return ""
    try:
        return str(_contained(root, rel))
    except _PathEscape:
        return ""


def _sanitize_selection_sources(sel_obj: Any, root: str) -> None:
    """Contain every per-element `source.file` in a selection, in place.

    The `source` block is produced INSIDE the previewed page — from a
    `data-kiro-source` attribute or a React Fiber `_debugSource` — so its
    `file` is preview-controlled in exactly the way `previewUrl` is. It is
    surfaced to the agent as "where the agent should edit", so a page that
    names `../../../../etc/hosts` (or any absolute path) would aim the edit
    outside the project the user registered.

    A path that escapes is cleared rather than dropped, and its confidence
    falls to `low` — the same shape `resolveSource()` emits when it finds
    nothing, so the agent locates the element by locator instead of trusting a
    file. Clearing only `file` would leave an incoherent `high`-confidence
    hint with no path.

    Absolute paths are handled by `_contained`: `os.path.join` lets an absolute
    candidate win, and the containment check then accepts it only if it is
    genuinely inside *root*. That keeps the normal React-Fiber case (absolute
    paths under the project) working.

    With no known root there is nothing to contain against, so every hint is
    cleared — fail closed.
    """
    if not isinstance(sel_obj, dict):
        return
    for el in sel_obj.get("elements") or []:
        if not isinstance(el, dict):
            continue
        src = el.get("source")
        if not isinstance(src, dict) or not src.get("file"):
            continue
        safe = _contained_source(root, str(src["file"])) if root else ""
        if not safe:
            src["file"] = ""
            src["confidence"] = "low"
        else:
            src["file"] = safe


def _read_request(fp: Path) -> dict | None:
    # Re-contain at entry so the read consumes the RETURN of the barrier even
    # when a caller built the path itself (idempotent for the glob/`_request_file`
    # callers; makes the containment legible at the sink).
    try:
        fp = _contained(DATA_DIR, os.fspath(fp))
        # Bound the read before it happens. A file in the queue dir is not
        # necessarily one this backend wrote through the size-capped API: the
        # bundled skill hands the agent this exact path, so anything with the
        # user's filesystem access is a writer here. `json.loads` on an oversized
        # file pulls the whole thing into memory and takes the backend down, and
        # `/queue` reads EVERY pending file, so one bad record poisons the route
        # rather than just itself. Treat over-limit as unreadable, which is the
        # same disposition this already gives malformed JSON.
        if fp.stat().st_size > MAX_RECORD_BYTES:
            return None
        req = json.loads(fp.read_text("utf-8"))
        return req if isinstance(req, dict) else None
    except (_PathEscape, OSError, ValueError):
        return None


def _atomic_write_json(path: Path, payload: dict, *, max_bytes: int | None = None) -> None:
    """Write JSON so an interrupted write can never leave a truncated file.

    An in-place `write_text` truncates the target first, so a SIGTERM partway
    through (the app being disabled mid-submit is the realistic case) leaves
    invalid JSON on disk. `_read_request` treats unparseable JSON as absent, so
    the user's queued request would silently disappear rather than fail loudly.
    Writing to a unique temp file in the SAME directory and `os.replace`-ing it
    over the target makes the swap atomic on POSIX and Windows alike, so a reader
    sees either the old contents or the new ones and never a half-written file.
    Mirrors `code_review_sage/sage_lib/learning.py::_atomic_write`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2).encode("utf-8")
    if max_bytes is not None and len(data) > max_bytes:
        # Refuse BEFORE the temp file exists, so the caller's rejection leaves the
        # previous record exactly as it was. Checking the serialized bytes here
        # rather than in the caller is what keeps the bound honest: this is the
        # only place that knows what will actually be written.
        raise _RecordTooLarge(
            f"record would be {len(data)} bytes, over the {max_bytes}-byte limit"
        )
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            # os.write is NOT guaranteed to write everything in one call — on a
            # nearly-full disk (or a signal) it returns a short count. Publishing
            # that with os.replace would swap a TRUNCATED file over good state,
            # which `_read_request` then treats as absent. Loop to completion and
            # fsync, so the rename only ever publishes a whole, durable file.
            written = 0
            while written < len(data):
                n = os.write(fd, data[written:])
                if n <= 0:  # pragma: no cover - defensive; os.write raises instead
                    raise OSError(f"short write to {tmp}: {written}/{len(data)} bytes")
                written += n
            os.fsync(fd)
        finally:
            os.close(fd)  # close even if the write raised
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_request(fp: Path, req: dict) -> None:
    # Same re-containment as `_read_request`: the write only ever touches a path
    # the barrier returned.
    fp = _contained(DATA_DIR, os.fspath(fp))
    # Bounded at the chokepoint rather than per route, because every mutating
    # route funnels through here — `/submit`, `/thread`, seal-on-send,
    # delete-comment and delivered can each be the append that crosses the line,
    # and a guard on only one of them would leave the others able to write a
    # record `_read_request` then reports as absent.
    _atomic_write_json(fp, req, max_bytes=MAX_RECORD_BYTES)


def _find_request(rid: str) -> Path | None:
    """Locate a request file by id, queue/ first then handled/."""
    for d in (QUEUE_DIR, HANDLED_DIR):
        try:
            fp = _request_file(d, rid)
        except _PathEscape:
            return None
        if fp.is_file():
            return fp
    return None


def _open_draft_file(project_id: str) -> Path | None:
    """The project's single open draft, if one exists.

    Uses the derived status, not raw `state`, so a request whose comments are
    already being worked can never quietly collect new comments.
    """
    for fp in _pending_files():
        req = _read_request(fp)
        if req and req.get("projectId") == project_id and _is_draft(req):
            return fp
    return None


def _valid_target(url: str) -> bool:
    """Only allow http://localhost[:port] or http://127.0.0.1[:port] (SSRF guard).

    The PORT is validated here too, not just the host. ``urlsplit`` parses
    ``http://localhost:notaport`` without complaint and resolves ``.hostname``
    fine -- ``.port`` is a lazily-parsed property, so the ValueError only surfaces
    at the first reader. Letting such a URL through the barrier persists it onto
    the project, and every later ``.port`` read (``_start_inject_proxy`` on the
    ``/projects`` poll) then raises: one bad input becomes a permanent
    self-inflicted 500 on project loading.
    """
    try:
        u = urlparse(url)
        if u.scheme != "http":
            return False
        if (u.hostname or "").lower() not in _LOOPBACK_HOSTS:
            return False
        port = u.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return False
    # `.port` already rejects >65535; port 0 parses but is not a dialable port.
    return port is None or 1 <= port <= 65535


def _valid_root(path: str):
    """Resolve a folder path to serve. Returns a normalised Path or None if it is
    not an existing directory or is a sensitive credential location.

    `os.path.realpath` rather than `Path.resolve()`: both collapse `..` and
    symlinks, but realpath is a *pure* normalisation while `resolve()` also
    counts as a filesystem access — so the old form was itself a path-injection
    sink on top of the ones it fed.

    Sensitivity is delegated to the shared `is_sensitive_path()` floor, not just
    a local dot-dir denylist: the preview servers will serve ANY file under the
    chosen folder, so picking `~/.kiro/crew` as a "project" would have exposed
    the governance trust root and this backend's own `.app_secret`.
    """

    try:
        real = os.path.realpath(os.path.expanduser(path))
    except (ValueError, OSError):
        return None
    if set(Path(real).parts) & _DENIED_ROOT_PARTS:
        return None
    if is_sensitive_path(real):
        return None
    # The reverse direction matters just as much here, because the preview
    # servers serve ANY file under the chosen folder: a root that merely
    # CONTAINS a credential store — `$HOME` itself, or any parent of `~/.ssh` —
    # turns that store into a fetchable URL for the previewed page's own script,
    # even though the root is not itself a sensitive path.
    if path_contains_sensitive(real):
        return None
    p = Path(real)
    # The `py/path-injection` finding on the next line is the FEATURE, and it is
    # NOT suppressed in code: GitHub's default-setup code scanning does not honour
    # `lgtm`-style inline markers, so a marker here would only mislead. It is
    # dismissed on GitHub with this rationale — an authenticated operator names a
    # folder on their own machine to preview it. The value is realpath-normalised
    # above (no traversal survives), screened against `_DENIED_ROOT_PARTS` and the
    # `is_sensitive_path()` floor, and there is no enclosing base to contain an
    # arbitrary user-chosen directory within.
    if not p.is_dir():
        return None
    return p


# Boot-time restore: resume serving the active project across restarts.
_boot_active = _active_project()
if _boot_active:
    _boot_root = _valid_root(_boot_active.get("path", ""))
    if _boot_root is not None:
        _ROOT = str(_boot_root)


# Extension → Content-Type for every file this backend serves FROM DISK.
#
# This map is closed and every value is a LITERAL, which is load-bearing twice:
#
#   * Security: the requested path is attacker-influenced, so deriving a header
#     value from it — as `mimetypes.guess_type(path)` did — put a tainted string
#     one step from `send_header`. That is `py/http-response-splitting`, and no
#     sanitiser call satisfies it because the analysis does not recognise one. A
#     dict lookup that can only ever return a constant breaks the flow outright
#     instead of trying to clean it.
#   * Behaviour: an unmapped extension now serves as `application/octet-stream`
#     rather than whatever the host's `/etc/mime.types` happened to say, so the
#     result no longer varies by machine and never sniffs a script type for a
#     file we do not recognise. `_header_value()` stays at the sink as defence in
#     depth for the proxied path, which does forward an upstream-selected value.
_CTYPE_OVERRIDES = {
    # markup + code
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".xml": "text/xml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".wasm": "application/wasm",
    ".pdf": "application/pdf",
    # images
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    # fonts
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    # media
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}

# What an extension we do not know is served as. Deliberately inert.
_CTYPE_DEFAULT = "application/octet-stream"


def _guess_ctype(p: Path) -> str:
    """Content-Type for a file served from disk — always a constant.

    A closed lookup, never a computation over the path (see `_CTYPE_OVERRIDES`).
    Only the file's extension is read, and only to pick a key.
    """
    return _CTYPE_OVERRIDES.get(p.suffix.lower(), _CTYPE_DEFAULT)


# Content types this backend will echo back for a proxied dev-server response.
# The upstream is the user's own dev server, but it is still an unaudited process
# whose headers land in OUR response, so its `Content-Type` is not forwarded
# verbatim — it only SELECTS a value from this table. Every value here is a
# literal, so a CR/LF (or a whole second header) smuggled into the upstream
# header can never reach `send_header` — that was `py/http-response-splitting`.
_PROXY_CTYPES: dict[str, str] = {
    "text/html": "text/html; charset=utf-8",
    "application/xhtml+xml": "text/html; charset=utf-8",
    "text/css": "text/css; charset=utf-8",
    "text/javascript": "text/javascript; charset=utf-8",
    "application/javascript": "text/javascript; charset=utf-8",
    "application/json": "application/json; charset=utf-8",
    "application/manifest+json": "application/manifest+json",
    "text/plain": "text/plain; charset=utf-8",
    "text/markdown": "text/markdown; charset=utf-8",
    "text/xml": "text/xml; charset=utf-8",
    "application/xml": "application/xml; charset=utf-8",
    "image/svg+xml": "image/svg+xml",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
    "image/avif": "image/avif",
    "image/x-icon": "image/x-icon",
    "image/vnd.microsoft.icon": "image/x-icon",
    "font/woff": "font/woff",
    "font/woff2": "font/woff2",
    "font/ttf": "font/ttf",
    "font/otf": "font/otf",
    "application/wasm": "application/wasm",
    "audio/mpeg": "audio/mpeg",
    "video/mp4": "video/mp4",
    "video/webm": "video/webm",
    "application/octet-stream": "application/octet-stream",
}


def _safe_upstream_ctype(raw: str | None, path: str) -> str:
    """A `Content-Type` safe to put in our own response for a proxied reply.

    Kept for the PROXIED path only, which is the one case where the value has to
    reflect something a foreign process said. The return is always one of the
    literals in `_PROXY_CTYPES` or `_CTYPE_OVERRIDES` (keyed by the requested
    extension when the upstream media type is unrecognised) — never a substring of
    *raw* — so nothing the dev server sends can be spliced into our header block.
    Files served from disk do not come through here: they use `_guess_ctype`.
    """
    media = (raw or "").split(";", 1)[0].strip().lower()
    mapped = _PROXY_CTYPES.get(media)
    if mapped:
        return mapped
    ext = os.path.splitext(urlparse(path).path)[1].lower()
    return _CTYPE_OVERRIDES.get(ext, _CTYPE_DEFAULT)


# A header NAME must be a bare RFC 7230 token — anything else is dropped rather
# than forwarded, so a malformed name cannot open a new header line of its own.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _header_value(value: str) -> str:
    """A response-header value stripped of anything that could split the response.

    Both places this backend writes a forwarded header value go through here, so
    the CR/LF/control strip lives in one spot — belt to `_safe_upstream_ctype`'s
    braces, and it also covers any header added here later.
    """
    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n")
    return cleaned[:256]


# ---------------------------------------------------------------------------
# Dev-server detection.
#
# A project registered as a folder may also be served by a dev server the user
# already has running. Rather than guessing from a list of popular ports — which
# picks the wrong server the moment two are up — identify it: every loopback
# listener has a PID, every PID has a working directory, and the one whose
# working directory sits inside the project folder IS that project's dev server.
#
# `lsof` is the only portable way to get that mapping without elevated
# privileges, and the two invocations below work identically on macOS and Linux.
# Detection is always best-effort: if lsof is missing or slow, callers fall back
# to the manual URL field.
# ---------------------------------------------------------------------------
_LSOF_TIMEOUT = 4  # generous: lsof on a busy machine can take ~1s
_PROBE_TIMEOUT = 1.5  # per-candidate HTTP probe


def _lsof_fields(args: list[str]) -> list[dict]:
    """Run lsof in field mode and return one dict per record.

    Field output is a flat stream of `p<pid>` / `f<fd>` / `n<name>` lines where
    the pid line begins a new process block, so `p` is carried forward.

    The binary is resolved from the fixed system directories rather than `PATH`:
    a gateway's `PATH` can lead with agent-writable dirs, so a bare `lsof` argv
    would let a planted shim run with our environment. `None` means unavailable,
    and the caller degrades to "no listeners found" rather than guessing.
    """
    lsof = trusted_system_bin("lsof")
    if not lsof:
        return []
    try:
        r = subprocess.run([lsof, *args], capture_output=True, text=True, timeout=_LSOF_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[dict] = []
    pid = ""
    for line in r.stdout.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            pid = val
        elif tag == "n":
            out.append({"pid": pid, "name": val})
    return out


def _loopback_listeners() -> dict[int, int]:
    """{port: pid} for TCP listeners bound to loopback (or all interfaces)."""
    found: dict[int, int] = {}
    for rec in _lsof_fields(["-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"]):
        name = rec["name"]
        if ":" not in name:
            continue
        host, _, port_s = name.rpartition(":")
        host = host.strip("[]")
        # `*` means all interfaces, which includes loopback.
        if host not in ("127.0.0.1", "localhost", "::1", "*", ""):
            continue
        try:
            port = int(port_s)
            found[port] = int(rec["pid"])
        except ValueError:
            continue
    return found


def _cwd_for_pids(pids: list[int]) -> dict[int, str]:
    """{pid: working directory} — one lsof call for the whole set."""
    if not pids:
        return {}
    joined = ",".join(str(p) for p in dict.fromkeys(pids))
    out: dict[int, str] = {}
    for rec in _lsof_fields(["-a", "-p", joined, "-d", "cwd", "-Fn"]):
        try:
            out[int(rec["pid"])] = rec["name"]
        except ValueError:
            continue
    return out


def _serves_html(port: int) -> bool:
    """Does this port answer with an HTML page?

    Discriminates a dev server from the API server / test runner / language
    server that may also be running out of the same folder. Deliberately lenient
    about status: a dev server can answer `/` with a 404 and still be the right
    target, so only the content type has to look like a page.
    """
    url = f"http://127.0.0.1:{port}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DesignTweak-Detect"})
        # The url is a literal f-string: scheme and host are constants and `port`
        # is an int parsed out of lsof output, so no scheme (`file://`) or host
        # can be substituted. `# noqa: S310` covers flake8-bandit only.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310
            return "text/html" in (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        return "text/html" in (exc.headers.get("Content-Type") or "").lower()
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _detect_dev_servers(root: Path, probe: bool = True) -> list[dict]:
    """Dev servers plausibly serving `root`, best match first.

    A candidate matches when the listening process's working directory is inside
    the project folder — which also covers a monorepo whose server runs from
    `<root>/apps/web`. Depth 0 (cwd IS the root) sorts first, since a server
    started in the project root is the more likely target than one nested in it.
    """
    listeners = _loopback_listeners()
    cwds = _cwd_for_pids(list(listeners.values()))

    out: list[dict] = []
    for port, pid in listeners.items():
        cwd = cwds.get(pid, "")
        if not cwd:
            continue
        try:
            depth = len(Path(cwd).resolve().relative_to(root).parts)
        except ValueError:
            continue  # cwd is not inside the project
        out.append(
            {
                "port": port,
                "pid": pid,
                "cwd": cwd,
                "depth": depth,
                "url": f"http://localhost:{port}",
                "servesHtml": _serves_html(port) if probe else None,
            }
        )
    # HTML-serving first, then shallowest cwd, then lowest port for stability.
    out.sort(key=lambda c: (c["servesHtml"] is False, c["depth"], c["port"]))
    return out


def _auto_dev_server(root: Path) -> str:
    """The one unambiguous dev-server URL for `root`, or "".

    Returns a URL only when exactly ONE candidate serves HTML. With none there is
    nothing to attach; with several, guessing would silently point the preview at
    the wrong server, so the caller surfaces the list instead.
    """
    html = [c for c in _detect_dev_servers(root) if c["servesHtml"]]
    return html[0]["url"] if len(html) == 1 else ""


# ---------------------------------------------------------------------------
# Dev-server processes we started.
#
# Spawned in their own process group so the whole tree can be signalled at once:
# `npm run dev` forks the real server as a child, so killing only the npm pid
# leaves an orphan holding the port. The isolation flag is platform-specific
# (`start_new_session` is POSIX-only, `CREATE_NEW_PROCESS_GROUP` is the Windows
# equivalent), and the teardown goes through `kill_process_tree` so Windows uses
# `taskkill /T` instead of the non-existent `os.killpg`. Registered with atexit
# so a gateway shutdown does not leak them either.
# ---------------------------------------------------------------------------
_DEV_PROCS: dict[str, dict] = {}  # projectId -> {proc, pgid, url, log}
_START_TIMEOUT = 45  # cold Vite/Next can take a while
_STOP_GRACE = 3


def _stop_dev_proc(project_id: str) -> bool:
    """Signal the whole process tree, escalating only if it ignores SIGTERM."""

    rec = _DEV_PROCS.pop(project_id, None)
    if not rec:
        return False
    _stop_inject_proxy(rec)
    proc = rec.get("proc")
    if proc is None:  # adopted server: proxy was ours, process is not
        return True
    try:
        kill_process_tree(proc.pid, SIGTERM)
        try:
            proc.wait(timeout=_STOP_GRACE)
        except Exception:  # noqa: BLE001
            kill_process_tree(proc.pid, SIGKILL)
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        pass  # already gone
    return True


def _stop_all_dev_procs() -> None:
    for pid in list(_DEV_PROCS):
        _stop_dev_proc(pid)


atexit.register(_stop_all_dev_procs)


def _dev_proc_alive(project_id: str) -> bool:
    rec = _DEV_PROCS.get(project_id)
    if not rec:
        return False
    proc = rec.get("proc")
    if proc is None:  # adopted: the user's server, our proxy
        return bool(rec.get("proxy"))
    return proc.poll() is None


# ---------------------------------------------------------------------------
# Injecting reverse proxy for dev servers
#
# The overlay is what makes select-to-edit work, and it is injected by THIS
# backend when it serves a folder from disk. Point the iframe straight at a dev
# server and the overlay never loads — Vite serves its own index.html, and no
# amount of postMessage plumbing helps because there is nothing in the page to
# talk to. Framing the dev server directly preserved hot reload and silently
# dropped the app's entire reason for existing.
#
# So a dev server is framed THROUGH a proxy that injects the overlay. Two
# decisions make this robust rather than a URL-rewriting game:
#
#   • It listens on its OWN port and maps paths 1:1. A dev server's HTML refers
#     to root-absolute URLs (/src/main.tsx, /@vite/client) and its client builds
#     more at runtime; behind a /proxy/<id>/ path prefix every one of them would
#     miss. Identity mapping means nothing needs rewriting but the script tag.
#   • WebSocket upgrades are relayed as raw bytes, so hot reload keeps working.
#     Once the handshake is done a WS connection is just a byte stream — we never
#     parse a frame, and the accept key is computed by the dev server, not us.
# ---------------------------------------------------------------------------
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
# Request headers that carry the BROWSER's credentials for the dashboard origin.
# They are stripped before relaying to the project's dev server (see
# `_relay_http`), and also from the WebSocket handshake replay.
_CREDENTIAL_REQUEST_HEADERS = {"cookie", "authorization"}
# Response headers the previewed project must never be able to set on OUR
# response. Cookies ignore ports, and this proxy shares 127.0.0.1 with the
# dashboard, so an upstream Set-Cookie could overwrite the dashboard session.
_CREDENTIAL_RESPONSE_HEADERS = {"set-cookie", "set-cookie2"}
# Served by the proxy itself, so the overlay needs no cross-origin fetch and no
# knowledge of which port this backend is on.
_OVERLAY_PATH = "/__kiro_select_to_edit__.js"
_WS_IDLE = 3600  # a quiet HMR socket is normal; don't tear it down

# Wall clock a single client read may block for. Without this every handler here
# inherits `socketserver`'s default of NO socket timeout, so a client that sends a
# permitted `Content-Length` and then no body parks its handler thread forever —
# and `ThreadingHTTPServer` gives one thread and one descriptor per connection, so
# repeating that pre-auth exhausts both. The WebSocket relay is unaffected: it
# pumps through `selectors.select()` and only `recv`s a socket already reported
# readable, and it carries its own `_WS_IDLE` cap for a legitimately quiet socket.
_CLIENT_READ_TIMEOUT = 30


class _IncompleteBody(ValueError):
    """A client declared a `Content-Length` it never delivered."""


_RELAY_TIMEOUT = 30


class _DevProxyHandler(BaseHTTPRequestHandler):
    """Byte-transparent reverse proxy, except HTML gains the overlay script."""

    protocol_version = "HTTP/1.1"
    timeout = _CLIENT_READ_TIMEOUT  # see `_CLIENT_READ_TIMEOUT`
    upstream_host = "127.0.0.1"
    upstream_port = 0
    # This proxy's OWN bound port, stamped on the per-proxy subclass once the
    # socket has one (it is ephemeral, so it cannot be known at class-creation
    # time). Used to keep an upstream redirect on this origin — see
    # `_keep_redirect_local`. Read from the subclass rather than
    # `self.server.server_address`, which is typed too loosely to index.
    proxy_port = 0

    def log_message(self, *args) -> None:
        pass

    def _upstream(self) -> str:
        return f"{self.upstream_host}:{self.upstream_port}"

    def _dispatch(self) -> None:
        if self.path.split("?", 1)[0] == _OVERLAY_PATH:
            return self._serve_overlay()
        if "websocket" in self.headers.get("Upgrade", "").lower():
            return self._relay_ws()
        return self._relay_http()

    do_GET = _dispatch
    do_POST = _dispatch
    do_HEAD = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch

    def _serve_overlay(self) -> None:
        try:
            js = INJECT_FILE.read_bytes()
        except OSError:
            js = b"// overlay not found"
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(js)))
        self.end_headers()
        self.wfile.write(js)

    def _relay_http(self) -> None:

        # Both directions are buffered whole (the HTML rewrite needs the full
        # body), so both need a ceiling: without one, a preview request or a
        # dev-server response larger than available memory takes the backend
        # down. The request side reuses MAX_BODY_BYTES (inbound payload cap) and
        # the response side MAX_STATIC_BYTES (one served asset), matching what
        # the non-proxy paths already enforce.
        body = b""
        length = self.headers.get("Content-Length")
        if length:
            try:
                size = int(length)
            except ValueError:
                size = -1
            if size > MAX_BODY_BYTES:
                # The unread body stays in the socket, so this connection can no
                # longer be framed — close rather than desync the next request.
                self.close_connection = True
                self.send_error(413, f"request body over the {MAX_BODY_BYTES}-byte proxy limit")
                return
            if size > 0:
                try:
                    body = self.rfile.read(size)
                except OSError:
                    # Timed out mid-body (see `_CLIENT_READ_TIMEOUT`). Forwarding a
                    # truncated body would make the dev server act on a partial
                    # request, so refuse and close.
                    self.close_connection = True
                    self.send_error(408, "request body timed out")
                    return
                if len(body) != size:
                    self.close_connection = True
                    self.send_error(400, "request body shorter than Content-Length")
                    return

        headers = {}
        for key, value in self.headers.items():
            low = key.lower()
            # Accept-Encoding is dropped so the upstream answers in identity
            # encoding — otherwise the HTML would arrive gzipped and the overlay
            # tag could not be inserted without decompressing it first.
            if low in _HOP_BY_HOP or low in ("accept-encoding", "host"):
                continue
            # Never forward the browser's credentials to the project's dev
            # server. This proxy advertises itself as http://127.0.0.1:<port>,
            # and cookies are host-scoped but PORT-agnostic — so when the
            # dashboard is served from 127.0.0.1 too, the browser attaches the
            # dashboard's SameSite=Lax auth cookie to these requests, and
            # relaying it verbatim would hand a usable session token to an
            # arbitrary `npm run dev` process. The upstream is the user's own
            # project and needs no dashboard credential to serve its assets.
            if low in _CREDENTIAL_REQUEST_HEADERS:
                continue
            headers[key] = value
        headers["Host"] = self._upstream()

        try:
            conn = http.client.HTTPConnection(
                self.upstream_host, self.upstream_port, timeout=_RELAY_TIMEOUT
            )
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            # Read one byte past the cap so an oversized body is detectable
            # without buffering all of it.
            payload = resp.read(MAX_STATIC_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            self.send_error(502, f"dev server unreachable: {exc}")
            return

        if len(payload) > MAX_STATIC_BYTES:
            conn.close()
            self.send_error(
                502, f"dev server response over the {MAX_STATIC_BYTES}-byte preview limit"
            )
            return

        if "text/html" in (resp.getheader("Content-Type") or "").lower():
            payload = _rewrite_html(payload, base=None, script=_OVERLAY_PATH)

        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in _HOP_BY_HOP or key.lower() == "content-length":
                continue
            # Never let the dev server set cookies on OUR response. Cookies are
            # host-scoped but PORT-agnostic, and this proxy shares 127.0.0.1 with
            # the dashboard — so an upstream `Set-Cookie` naming the gateway's own
            # cookie would REPLACE the dashboard's session in the browser, logging
            # the user out or pinning them to an attacker-chosen value. This is the
            # response-side mirror of the request-side `_CREDENTIAL_REQUEST_HEADERS`
            # strip: the previewed project has no business touching dashboard state
            # in either direction.
            if key.lower() in _CREDENTIAL_RESPONSE_HEADERS:
                continue
            # Upstream is the project's own dev server, but its headers land in
            # OUR response: a folded or CR/LF-bearing value forwarded verbatim
            # would let it append headers or a second body (response splitting).
            if not _HEADER_NAME_RE.match(key):
                continue
            # A redirect naming the dev server's OWN origin would take the iframe
            # off this proxy and onto the bare upstream port — and because cookies
            # are host-scoped but PORT-agnostic, the browser would then attach the
            # dashboard's session cookie to the project's `npm run dev` process.
            # The request-side strip cannot help: that navigation never passes
            # through here. So keep the redirect INSIDE the proxy by re-pointing
            # it at our own origin, which preserves the redirect's behaviour while
            # keeping the cookie boundary intact.
            if key.lower() == "location":
                value = self._keep_redirect_local(value)
            self.send_header(key, _header_value(value))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        conn.close()

    def _keep_redirect_local(self, location: str) -> str:
        """Re-point an upstream redirect that names the dev server at THIS proxy.

        Only a `Location` whose host is loopback AND whose port is the upstream's
        is rewritten — that is the one case where following it would move the
        iframe from the proxy's port to the dev server's while staying on the same
        cookie host. A redirect that is already relative needs nothing (it resolves
        against our origin), and an off-host absolute redirect is left alone: the
        dashboard cookie is scoped to this host, so it cannot ride along.

        Path, query and fragment are preserved verbatim; only the authority moves.
        """
        try:
            parts = urlparse(location)
            if not parts.scheme and not parts.netloc:
                return location  # relative — already ours
            if parts.scheme.lower() not in ("http", "https"):
                return location
            if (parts.hostname or "").lower() not in _LOOPBACK_HOSTS:
                return location
            # `.port` is lazily parsed, so a non-numeric authority raises HERE
            # rather than at `urlparse` — the same seam `_valid_target` guards.
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        except ValueError:
            return location  # unparseable: forward as-is rather than invent a target
        if port != self.upstream_port:
            return location
        rest = parts.path or "/"
        if parts.query:
            rest += "?" + parts.query
        if parts.fragment:
            rest += "#" + parts.fragment
        return f"http://127.0.0.1:{self.proxy_port}{rest}"

    def _relay_ws(self) -> None:

        try:
            up = socket.create_connection((self.upstream_host, self.upstream_port), timeout=10)
        except OSError:
            self.send_error(502, "dev server unreachable")
            return

        # Replay the handshake verbatim; the upstream's 101 comes back sanitized
        # below, so we never compute Sec-WebSocket-Accept ourselves.
        lines = [f"{self.command} {self.path} HTTP/1.1"]
        for key, value in self.headers.items():
            if key.lower() in _CREDENTIAL_REQUEST_HEADERS:
                continue  # same reason as _relay_http: never leak dashboard creds
            lines.append(f"{key}: {self._upstream() if key.lower() == 'host' else value}")
        try:
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
        except OSError:
            up.close()
            return

        self.close_connection = True
        down = self.connection

        # Read the upstream handshake BEFORE any byte pumping. `_relay_http`
        # already strips `_CREDENTIAL_RESPONSE_HEADERS` from ordinary responses,
        # but a 101 used to go straight into the pump below and bypass that
        # filter entirely. Same root cause as the request side: cookies ignore
        # ports and this proxy shares 127.0.0.1 with the dashboard, so an
        # upstream `Set-Cookie` naming the gateway's cookie would replace the
        # user's dashboard session. Bounded read — a dev server that never
        # terminates its header block must not hang us or grow this unboundedly.
        head = b""
        up.settimeout(_RELAY_TIMEOUT)
        try:
            while b"\r\n\r\n" not in head and len(head) < 65536:
                part = up.recv(4096)
                if not part:
                    break
                head += part
        except OSError:
            up.close()
            return
        raw_head, sep, rest = head.partition(b"\r\n\r\n")
        if not sep:
            up.close()
            self.send_error(502, "malformed upstream handshake")
            return
        head_lines = raw_head.split(b"\r\n")
        sanitized = [head_lines[0]]  # status line verbatim — we never recompute Accept
        for header_line in head_lines[1:]:
            name, _, _value = header_line.partition(b":")
            if name.strip().lower().decode("latin-1", "replace") in _CREDENTIAL_RESPONSE_HEADERS:
                continue
            sanitized.append(header_line)
        try:
            down.sendall(b"\r\n".join(sanitized) + b"\r\n\r\n")
            if rest:
                down.sendall(rest)  # frames that arrived in the same read
        except OSError:
            up.close()
            return

        up.settimeout(None)
        down.settimeout(None)
        sel = selectors.DefaultSelector()
        sel.register(up, selectors.EVENT_READ, down)
        sel.register(down, selectors.EVENT_READ, up)
        try:
            while True:
                events = sel.select(timeout=_WS_IDLE)
                if not events:
                    return  # idle past the cap
                for sel_key, _mask in events:
                    src_sock = cast(socket.socket, sel_key.fileobj)
                    dst_sock = cast(socket.socket, sel_key.data)
                    try:
                        chunk = src_sock.recv(65536)
                    except OSError:
                        return
                    if not chunk:
                        return  # either side hung up
                    try:
                        dst_sock.sendall(chunk)
                    except OSError:
                        return
        finally:
            sel.close()
            try:
                up.close()
            except OSError:
                pass


def _start_inject_proxy(dev_url: str) -> tuple[object | None, str]:
    """Front `dev_url` with an overlay-injecting proxy. Returns (server, url)."""
    parsed = urlparse(dev_url)
    host = parsed.hostname or "127.0.0.1"
    # Second `.port` reader after `_valid_target`. Guarded independently because a
    # URL can reach here from `_auto_dev_server` / `_start_dev_proc` as well as
    # from a persisted, allow-listed one -- and an unguarded ValueError here is
    # what turned a malformed persisted port into a permanent `/projects` 500.
    # Failing to (None, "") makes the caller frame the bare URL instead.
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None, ""
    bound = cast(
        "type[_DevProxyHandler]",
        type("_BoundDevProxy", (_DevProxyHandler,), {"upstream_host": host, "upstream_port": port}),
    )
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    except OSError:
        return None, ""
    srv.daemon_threads = True
    # The bound port only exists after the socket is bound, so stamp it on this
    # proxy's own handler subclass now. Per-proxy, never shared.
    bound.proxy_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True, name="kiro-dev-proxy").start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


# ---------------------------------------------------------------------------
# Static preview server (loopback, ephemeral, unauthenticated by design).
#
# WHY THIS EXISTS — it is a security boundary, not a convenience.
#
# The preview iframe used to be pointed at `/apps/design-tweak/api/proxy/<id>/`,
# which is served from the DASHBOARD's own origin. The frame needs
# `allow-scripts` (prototypes are JavaScript) and `allow-same-origin` (real
# projects use localStorage / cookies / same-origin fetch, and every asset the
# page loads must be reachable) — but those two together, on our OWN origin,
# cancel the sandbox: arbitrary previewed project HTML (or a third-party snippet
# pasted into a mockup) would run first-party and could call the authenticated
# dashboard API and read the parent DOM.
#
# Dropping `allow-same-origin` is NOT the fix. It gives the frame an opaque
# origin, whose "site for cookies" is null, so the browser treats every
# subresource request as cross-site and withholds the dashboard's SameSite=Lax
# auth cookie. Measured in Chromium: the frame's own navigation still gets the
# cookie, but the overlay script, CSS, images and `fetch()` all lose it, and
# `localStorage` throws SecurityError. The gateway would 401 the overlay itself,
# so select-to-edit would not merely degrade — it would not load.
#
# So the frame keeps the permissive sandbox and instead stops sharing our origin:
# the project is served from 127.0.0.1 on an ephemeral port, exactly like the
# dev-server path already is. `allow-same-origin` then grants the project only
# ITS OWN origin, which is what a dev server would have given it anyway.
#
# Consequences that are deliberate:
#   * This listener is UNAUTHENTICATED. It is the same exposure `npm run dev`
#     already accepts: loopback-only bind, an OS-assigned random port, and it
#     serves nothing but the folders the user explicitly registered, each behind
#     the `_contained` barrier.
#   * It reads no request credentials and forwards nothing anywhere — there is no
#     upstream. Unlike `_DevProxyHandler` there is no relay to strip headers on.
#   * The port is never persisted; it dies with this backend and is resolved live
#     in `_h_projects_list`, so a stale port can never be framed.
# ---------------------------------------------------------------------------
class _StaticInjectHandler(BaseHTTPRequestHandler):
    """Serve any registered project folder from loopback, overlay injected.

    Paths are `/<projectId>/<rest>`, mapping 1:1 onto the registered folder, so
    one listener covers every project and a newly registered folder needs no
    restart. Read-only: anything but GET/HEAD is refused.
    """

    protocol_version = "HTTP/1.1"
    timeout = _CLIENT_READ_TIMEOUT  # see `_CLIENT_READ_TIMEOUT`

    def log_message(self, *args) -> None:
        pass

    def _refuse(self, status: int, msg: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(msg)

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        # The previewed project is NOT allowed to become a cross-origin API for
        # some other page: no CORS headers are emitted, so only the frame itself
        # (same origin as this server) can read these bodies.
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self) -> None:
        path = urlparse(self.path).path
        if path == _OVERLAY_PATH:
            try:
                js = INJECT_FILE.read_bytes()
            except OSError:
                js = b"// overlay not found"
            return self._send(200, "application/javascript; charset=utf-8", js)
        rel = unquote(path).lstrip("/")
        first, _, rest = rel.partition("/")
        # This listener is dedicated to exactly ONE project (bound at server
        # creation, see `_static_preview_base`) — it does not consult the full
        # `_CFG["projects"]` list. Two projects previewed from disk therefore
        # never share a listener, and so never share the browser-storage
        # origin (scheme+host+port) that listener answers on: cross-project
        # localStorage/cookie access is impossible because there is no
        # same-origin path between them, not merely an unenforced convention.
        bound_id: str = getattr(self.server, "kiro_project_id", "")
        if first != bound_id:
            return self._refuse(404, b"unknown project")
        proj = next((p for p in _CFG["projects"] if p["id"] == first), None)
        if proj is None:
            return self._refuse(404, b"unknown project")
        root = _valid_root(proj["path"])
        if root is None:
            return self._refuse(404, b"project folder no longer readable")
        # `base` is this server's own 1:1 prefix for the project, so a nested
        # entry's <base href> and the diagnostic 404's links stay correct.
        status, ctype, body = _static_response(str(root), rest, f"/{first}/", script=_OVERLAY_PATH)
        return self._send(status, ctype, body)

    do_GET = _dispatch
    do_HEAD = _dispatch

    def do_POST(self) -> None:  # noqa: N802
        self._refuse(405, b"read-only preview server")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST


# One live static preview server PER PROJECT, keyed by project id:
# `{project_id: {"srv": ThreadingHTTPServer, "url": str}}`. Each project gets
# its own ephemeral loopback port — and therefore its own browser-storage
# origin — rather than sharing one listener differentiated only by URL path.
# A shared listener put every previewed project's localStorage/cookies on the
# SAME origin (scheme+host+port; the path prefix plays no part in same-origin
# checks), so switching the panel from project A to project B on that one
# listener handed B's page A's storage. See the design_tweak security-review
# thread for the reachable vector.
_STATIC_SRV: dict[str, dict] = {}


def _static_preview_base(project_id: str) -> str:
    """Base URL of *project_id*'s dedicated loopback static server.

    Starts that project's own listener on demand. Returns "" if it cannot
    bind. There is deliberately NO fallback: the panel renders its "preview
    not reachable" state instead. The gateway-proxied route this used to fall
    back to served project files from the DASHBOARD's origin, which let a
    hostile previewed page escape the iframe sandbox, so it has been deleted
    (it answers 410). Do not reintroduce a same-origin fallback.
    """
    rec = _STATIC_SRV.get(project_id)
    if rec and rec.get("url"):
        return str(rec["url"])
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _StaticInjectHandler)
    except OSError:
        return ""
    srv.daemon_threads = True
    # Bind the project this listener is dedicated to onto the server instance
    # itself — `_StaticInjectHandler._dispatch` reads it back via `self.server`
    # so the SAME handler class stays generic while each instance answers for
    # exactly one project.
    srv.kiro_project_id = project_id  # type: ignore[attr-defined]
    threading.Thread(
        target=srv.serve_forever, daemon=True, name=f"kiro-static-preview-{project_id}"
    ).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    _STATIC_SRV[project_id] = {"srv": srv, "url": url}
    return url


def _stop_static_preview(project_id: str) -> None:
    """Shut down and forget *project_id*'s dedicated static listener.

    Each project gets its OWN listener (a shared one would put two projects on
    one browser-storage origin), so the registry entry is the only handle to it.
    Dropping the project without this leaves the thread and the bound port alive
    with nothing able to reach them, and `daemon_threads` only helps at process
    exit — a long-lived gateway accumulates one orphan per remove.
    """
    rec = _STATIC_SRV.pop(project_id, None)
    if rec is None:
        return
    srv = rec.get("srv")
    if srv is None:
        return
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:  # noqa: BLE001
        pass


def _static_preview_url(project_id: str) -> str:
    """URL the iframe should use to preview `project_id` from disk ("" if none)."""
    base = _static_preview_base(project_id)
    return f"{base}{project_id}/" if base else ""


def _stop_inject_proxy(rec: dict) -> None:
    srv = rec.get("proxy")
    if srv is None:
        return
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:  # noqa: BLE001
        pass
    rec["proxy"] = None
    # Keep "proxyUrl is set" ⟺ "proxy is live": a dead listener's URL must never
    # be reused as a cache hit below, nor framed by `_h_projects_list`.
    rec["proxyUrl"] = ""
    rec["proxyFor"] = ""


def _front_with_proxy(project_id: str, dev_url: str) -> str:
    """Attach an injecting proxy to a running dev server; return the URL to frame.

    Idempotent: if this project already has a LIVE proxy fronting this same
    `dev_url`, that proxy is reused instead of being rebuilt. `_h_projects_list`
    calls this on every poll for a project whose dev URL is persisted, so a
    start-unconditionally version would leak a listener per request and hand the
    iframe a fresh origin each time (losing the preview's localStorage and
    scroll position on every refresh).

    Returns `""` when the proxy cannot be started, which the panel renders as the
    unreachable state. Framing the BARE dev server instead is not an acceptable
    degradation, even though it is also loopback: cookies are host-scoped but
    **port-agnostic**, so `127.0.0.1:<dev-port>` receives the dashboard's own
    session cookie. Stripping `Cookie`/`Authorization` is one of the reasons this
    proxy exists, so falling back past it would hand the previewed project's code
    the credential the proxy was built to withhold — trading a fixed
    credential-exposure bug for a missing overlay. A preview that says it is
    unreachable is better than one that leaks.
    """
    rec = _DEV_PROCS.get(project_id)
    if rec is not None and rec.get("proxy") is not None and rec.get("proxyFor") == dev_url:
        cached = str(rec.get("proxyUrl") or "")
        if cached:
            return cached
    srv, url = _start_inject_proxy(dev_url)
    if not url:
        return ""
    if rec is not None:
        _stop_inject_proxy(rec)
        rec["proxy"] = srv
        rec["proxyUrl"] = url
        rec["proxyFor"] = dev_url
    else:
        _DEV_PROCS[project_id] = {
            "proc": None,
            "pgid": None,
            "url": dev_url,
            "proxy": srv,
            "proxyUrl": url,
            "proxyFor": dev_url,
            "adopted": True,
        }
    return url


def _start_dev_proc(project_id: str, root: Path) -> dict:
    """Start the project's dev server and wait until it is listening.

    Returns `{"ok": True, "url": …}` or `{"ok": False, "error": …, "log": …}`.

    The port is NOT chosen here — the dev tool picks its own, and we then find it
    by matching a listening port back to a process rooted in this folder. That
    avoids a per-framework table of port flags, and it is also what makes the
    result honest: we report the port something is actually listening on.
    """
    if _dev_proc_alive(project_id):
        rec = _DEV_PROCS[project_id]
        return {
            "ok": True,
            "url": rec.get("proxyUrl") or rec["url"],
            "devUrl": rec["url"],
            "already": True,
        }
    _stop_dev_proc(project_id)  # clear a dead record

    cmd = _dev_command(root)
    if not cmd:
        return {
            "ok": False,
            "error": "No dev script found in package.json (looked for: "
            + ", ".join(_DEV_SCRIPTS)
            + ").",
        }

    # Resolve the package manager absolutely — the gateway hands this backend a
    # minimal PATH, so spawning by bare name fails with ENOENT even though the
    # same command works in a terminal.
    binary = _resolve_bin(cmd[0])
    if binary is None:
        looked = ", ".join(str(d) for d in _node_bin_dirs()[:4])
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
            env=_child_env(binary.parent),  # node must be on PATH for npm's own child
            # Own process group → killable as a tree. `start_new_session` is a
            # POSIX-only setsid() and raises on Windows; the Windows equivalent
            # is the creation flag, which is 0 on POSIX.
            start_new_session=IS_POSIX,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"could not start `{' '.join(cmd)}`: {exc}"}

    # POSIX-only: there is no process-group id on Windows, where descendants are
    # matched by walking the parent chain instead (see `_in_proc_tree`).
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

    # Poll for the port it chose. Probing is skipped while polling: an HTTP request
    # per candidate per tick is wasteful, and a dev server that is listening but
    # still compiling would fail the HTML check and look like a miss.
    deadline = time.time() + _START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:  # exited — surface its own output
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
        for cand in _detect_dev_servers(root, probe=False):
            if cand["pid"] == proc.pid or _in_proc_tree(cand["pid"], proc.pid, pgid):
                _DEV_PROCS[project_id]["url"] = cand["url"]
                # Frame the PROXY, not the dev server: the proxy is what injects
                # the select-to-edit overlay.
                framed = _front_with_proxy(project_id, cand["url"])
                return {
                    "ok": True,
                    "url": framed,
                    "devUrl": cand["url"],
                    "port": cand["port"],
                    # Empty `framed` means the proxy could not bind, so nothing is
                    # framed at all — that is not "injected".
                    "injected": bool(framed) and framed != cand["url"],
                }
        time.sleep(0.4)

    _stop_dev_proc(project_id)
    return {
        "ok": False,
        "error": f"`{' '.join(cmd)}` did not start listening within {_START_TIMEOUT}s.",
    }


# Depth cap on the Windows parent-chain walk in `_in_proc_tree`: a real chain is
# npm → node → (maybe) a wrapper, and the cap keeps a corrupt/cyclic parent map
# from spinning even though `seen` already breaks true cycles.
_PROC_TREE_MAX_DEPTH = 16


def _in_proc_tree(pid: int, root_pid: int, pgid: int | None) -> bool:
    """Does `pid` belong to the tree we started? The listener is usually npm's CHILD.

    POSIX matches on the process group, which catches a grandchild even when the
    intermediate `npm` has already exited. Windows has no process group to read,
    so the parent chain is walked instead — bounded because a real chain here is
    two or three links and `get_ppid` returns 0 at the root.
    """
    if pgid is not None:
        try:
            return os.getpgid(pid) == pgid
        except OSError:
            return False
    seen: set[int] = set()
    cur = pid
    for _ in range(_PROC_TREE_MAX_DEPTH):
        if cur == root_pid:
            return True
        if cur <= 0 or cur in seen:
            return False
        seen.add(cur)
        cur = get_ppid(cur)
    return False


#
# Not every project folder has index.html at its top level: a repo may keep the
# static site in public/ or dist/, or nest the app in a subfolder (mono-repos).
# Rather than 404 on the folder request, look for the most likely entry file,
# and if there is none, render a page that lists the HTML files we DID find so
# the user can pick one instead of staring at a dead iframe.
# ---------------------------------------------------------------------------
_ENTRY_CANDIDATES = (
    "index.html",
    "index.htm",
    "public/index.html",
    "dist/index.html",
    "build/index.html",
    "out/index.html",
    "app/index.html",
    "src/index.html",
    "site/index.html",
    "www/index.html",
    "docs/index.html",
    "demo/index.html",
    "example/index.html",
    "examples/index.html",
)

# Directories that never contain the previewable entry point but do contain
# thousands of files — skipping them keeps the HTML scan fast.
_SCAN_SKIP_DIRS = {
    "node_modules",
    ".git",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".cache",
    ".turbo",
    "__pycache__",
    ".venv",
    "venv",
    "coverage",
    "htmlcov",
    ".pytest_cache",
    "target",
    "vendor",
    ".idea",
    ".vscode",
}

# A module script pointing at TypeScript/JSX is a BUNDLER TEMPLATE, not a page.
# Browsers cannot execute .ts/.tsx/.jsx, so serving such an index.html statically
# yields HTTP 200, valid HTML, and a completely blank render — the worst kind of
# failure, because every layer reports success. Detect it and say so instead.
#
# Attributes are checked independently of order: `<script type="module" src=…>`
# and `<script src=… type="module">` are both valid HTML and both appear in real
# templates, so a single ordered pattern silently misses half of them.
_SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_ATTR_TYPE_MODULE_RE = re.compile(r"""\btype\s*=\s*["']module["']""", re.IGNORECASE)
_ATTR_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_UNBUNDLED_EXTS = (".ts", ".tsx", ".jsx")


def _unbundled_entry(html: bytes) -> str:
    """The first TS/JSX module script in this page, or "" if it can stand alone."""
    try:
        text = html.decode("utf-8", "replace")
    except (UnicodeDecodeError, AttributeError):
        return ""
    for m in _SCRIPT_TAG_RE.finditer(text):
        attrs = m.group(1)
        if not _ATTR_TYPE_MODULE_RE.search(attrs):
            continue
        src_m = _ATTR_SRC_RE.search(attrs)
        if not src_m:
            continue  # inline module — nothing to fetch
        src = src_m.group(1)
        bare = src.split("?")[0].split("#")[0].lower()
        if bare.endswith(_UNBUNDLED_EXTS):
            return src
    return ""


# Where Node toolchains actually live. The gateway spawns this backend with a
# minimal PATH — typically /usr/bin:/bin:/usr/sbin:/sbin — so `npm` is NOT
# resolvable by name even though it works fine in the user's terminal. Two things
# follow: the binary has to be found absolutely, AND the child's PATH has to
# include its directory, because `npm run dev` shells out to `node` itself.
_NODE_BIN_DIRS = (
    "/opt/homebrew/bin",  # homebrew, Apple silicon
    "/usr/local/bin",  # homebrew (Intel) / manual installs
    "/opt/local/bin",  # MacPorts
    "/usr/bin",
    "~/.volta/bin",
    "~/.bun/bin",
    "~/.asdf/shims",
    "~/.local/share/fnm/aliases/default/bin",
    "~/Library/pnpm",
)
# nvm keeps a directory per version; prefer the newest.
_NVM_GLOB = "~/.nvm/versions/node/*/bin"


def _node_bin_dirs() -> list[Path]:
    dirs = [Path(d).expanduser() for d in _NODE_BIN_DIRS]
    try:

        nvm = sorted(glob.glob(os.path.expanduser(_NVM_GLOB)), reverse=True)
        dirs = [Path(p) for p in nvm] + dirs
    except OSError:
        pass
    return [d for d in dirs if d.is_dir()]


def _resolve_bin(name: str) -> Path | None:
    """Absolute path to a package-manager binary, or None.

    `shutil.which` is tried first so a properly-configured PATH wins; the
    directory scan is the fallback for the gateway's stripped environment.
    """

    found = shutil.which(name)
    if found:
        return Path(found)
    for d in _node_bin_dirs():
        cand = d / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


# Environment this backend holds that the user's dev script must NEVER see.
# `KIROCREW_PROXY_SECRET` is the whole of this backend's authentication: the
# gateway HMACs every proxied request with it (`kiro_crew.apps.proxy_auth`), and
# `_authorized()` refuses anything unsigned. Handing it to a project's `npm run
# dev` — arbitrary code from a folder the operator merely pointed at — would let
# that code forge signed calls straight to our loopback socket and bypass the
# gateway's token auth and per-app scope entirely.
#
# `PORT` is stripped because `minimal_env()` sets it to THIS backend's port
# (`apps/backend.py`); a dev server that honours `PORT` (Next, CRA, many Node
# servers do) would try to bind a socket we already hold and die on EADDRINUSE.
#
# `SSH_AUTH_SOCK` / `GIT_SSH_COMMAND` / `GIT_SSH` are stripped because the dev
# script is untrusted project code, not Kiro Crew's own: an inherited SSH agent
# socket or SSH override command lets it authenticate to a remote (`git push`,
# a bare `ssh`) AS the operator, with no confinement of its own — the same
# credential class `sandbox._SENSITIVE_ENV_PREFIXES` strips for the sandboxed
# agent spawn path, kept here as its own narrow entry rather than an import
# across modules for a single-purpose backend that otherwise has none.
_CHILD_ENV_STRIP = (
    "KIROCREW_PROXY_SECRET",
    "PORT",
    "NODE_OPTIONS",
    "SSH_AUTH_SOCK",
    "GIT_SSH_COMMAND",
    "GIT_SSH",
)
# Prefixes covering every capability/identity var the host may inject
# (`KIROCREW_HOME`, `KIROCREW_APP_*`, operator-declared `KIROCREW_DEVFLEET_BIN_*`,
# `KIROCREW_PROJECT_DIR`, …). None of them are the child's business, and a
# forward-compatible prefix strip means a var added upstream later cannot leak
# through this seam by default.
_CHILD_ENV_STRIP_PREFIXES = ("KIROCREW_", "KIRO_CREW_")


def _child_env(bin_dir: Path) -> dict:
    """Environment for the dev server: ours, minus our secrets, toolchain on PATH.

    The child is UNTRUSTED code (the project's own dev script), so this is a
    deny-by-prefix strip rather than a hand-maintained blocklist — see
    `_CHILD_ENV_STRIP` for why `KIROCREW_PROXY_SECRET` and `PORT` in particular
    are disqualifying.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _CHILD_ENV_STRIP and not k.startswith(_CHILD_ENV_STRIP_PREFIXES)
    }
    extra = [str(bin_dir)] + [str(d) for d in _node_bin_dirs()]
    seen, parts = set(), []
    for p in extra + os.environ.get("PATH", "").split(os.pathsep):
        if p and p not in seen:
            seen.add(p)
            parts.append(p)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _pkg_scripts(root: Path) -> dict:
    try:
        data = json.loads((root / "package.json").read_text("utf-8"))
        s = data.get("scripts")
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


# Lockfile → the package manager that project expects. Order matters: a repo can
# carry more than one, and the more specific manager wins over npm's default.
_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("bun.lockb", "bun"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)
# Script names that mean "run the dev server", best first.
_DEV_SCRIPTS = ("dev", "start:dev", "dev:web", "serve", "start")


def _dev_command(root: Path) -> list[str]:
    """The command that starts this project's dev server, or [] if none is obvious.

    Deliberately does NOT pass a port: the flag differs per framework (`--port` for
    Vite, `-p` for Next, …) and guessing wrong just fails. Let the tool choose its
    own port and find it afterwards with `_detect_dev_servers`.
    """
    scripts = _pkg_scripts(root)
    script = next((s for s in _DEV_SCRIPTS if s in scripts), "")
    if not script:
        return []
    pm = next((m for f, m in _LOCKFILES if (root / f).is_file()), "npm")
    return [pm, "run", script] if pm != "bun" else ["bun", "run", script]


def _classify_project(root: Path) -> dict:
    """Can this folder be previewed from disk, or does it need a dev server?

    The distinction is not "does it have an index.html" — a Vite project has one,
    and serving it statically yields a blank page because its only script is
    TypeScript. So: resolve the entry, and if that entry is a bundler template,
    the folder needs its dev server.
    """
    entry = _find_entry(root)
    unbundled = ""
    if entry is not None:
        try:
            unbundled = _unbundled_entry(entry.read_bytes())
        except OSError:
            unbundled = ""
    cmd = _dev_command(root)
    needs = bool(unbundled) or (entry is None and bool(cmd))
    return {
        "needsDevServer": needs,
        "devCommand": " ".join(cmd),
        # Named so the panel can explain WHY rather than just asserting it.
        "unbundledEntry": unbundled,
        "hasEntry": entry is not None,
    }


def _find_entry(folder: Path, root: Path | None = None):
    """Best-guess entry HTML inside `folder`. Returns a Path or None.

    Every candidate is screened by the SAME three secret barriers the direct-file
    path uses. That is load-bearing rather than belt-and-braces: `_static_response`
    runs those checks on the requested path, which for a directory request is the
    DIRECTORY, and then calls this to pick an entry afterwards. A project
    containing `index.html -> .env` therefore passed the checks on the folder and
    got the symlink's target served — the candidate itself was never screened.
    `_contained` already resolves the link, so screening its return value closes
    it. `root` defaults to `folder` when a caller has no separate project root.
    """
    base = root if root is not None else folder
    for rel in _ENTRY_CANDIDATES:
        # Re-contain per candidate so the stat consumes the barrier's return
        # value regardless of what the caller passed as `folder`.
        try:
            p = _contained(folder, rel)
        except _PathEscape:
            continue
        if not p.is_file():
            continue
        if is_sensitive_path(str(p)) or _is_project_secret(base, p) or _is_kirocrew_internal(p):
            continue
        return p
    return None


def _scan_html(root: Path, limit: int = 40, max_depth: int = 3) -> list[str]:
    """Shallow scan for .html/.htm files, returned as root-relative POSIX paths.

    Feeds the diagnostic 404 page, so every name it collects is DISCLOSED to the
    previewed page. Symlinks are skipped outright — not followed, and not listed:
    `e.is_dir()` follows the link, so a project containing `docs -> ~/.ssh` would
    otherwise have this walk enumerate a protected directory and print the
    filenames it found there. Only filenames leak, never contents, but the
    directory listing is exactly what the sensitive-path floor exists to withhold.

    Skipping rather than resolving-and-containing is deliberate: a legitimate
    preview never needs a symlinked entry page, so refusing the whole class is
    cheaper than proving each target safe.
    """
    found: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if len(found) >= limit or depth > max_depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: (e.is_dir(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if len(found) >= limit:
                return
            # Before `is_dir()`, which would follow the link.
            if e.is_symlink():
                continue
            name = e.name
            if e.is_dir():
                if name.startswith(".") or name in _SCAN_SKIP_DIRS:
                    continue
                walk(e, depth + 1)
            elif e.suffix.lower() in (".html", ".htm"):
                try:
                    found.append(e.relative_to(root).as_posix())
                except ValueError:
                    continue

    walk(root, 0)
    return found


def _rewrite_html(
    body: bytes, base: str | None = PROXY_PUBLIC_BASE, script: str = INJECT_PUBLIC
) -> bytes:
    """Inject the Select-to-Edit overlay, and optionally a <base> tag.

    `base=None` is for the dev-server proxy, which maps paths 1:1 and so needs no
    <base> — adding one there would repoint every relative URL and break the page.
    """
    try:
        html = body.decode("utf-8", "replace")
    except (UnicodeDecodeError, AttributeError):
        return body
    inject_tag = f'<script src="{script}"></script>'
    if base is not None:
        base_tag = f'<base href="{base}">'
        low = html.lower()
        head = low.find("<head")
        if head != -1:
            end = low.find(">", head)
            if end != -1:
                html = html[: end + 1] + base_tag + html[end + 1 :]
        else:
            html = base_tag + html
    bidx = html.lower().rfind("</body>")
    if bidx != -1:
        html = html[:bidx] + inject_tag + html[bidx:]
    else:
        html = html + inject_tag
    return html.encode("utf-8")


# ---------------------------------------------------------------------------
# Static serving, as pure response builders.
#
# `_StaticInjectHandler` — the ephemeral loopback server the PREVIEW IFRAME is
# pointed at (see `_static_preview_base`) — is now the ONLY caller. The former
# gateway-proxied `/apps/<app>/api/proxy/…` route was deleted: serving
# project-controlled content from the dashboard's own origin let a hostile
# previewed page escape the iframe sandbox. Keep these as pure builders anyway —
# entry-point resolution, the containment barrier, the bundler-template
# explanation and the diagnostic 404 are worth stating once, and the loopback
# server for dev projects (`_DevProxyHandler`) shares the same rules.
# Each returns `(status, content_type, body)` and touches no request state.
# ---------------------------------------------------------------------------
def _needs_dev_server_body(root: Path, page: Path, entry: str) -> bytes:
    """The page is a bundler template — explain that, don't render a blank.

    `<script type="module" src="…/main.tsx">` needs Vite (or equivalent) to
    transform it. Served from disk the browser gets TypeScript, refuses to
    execute it, and leaves an empty `#root`: HTTP 200, valid HTML, nothing on
    screen. Silent success is the worst outcome here, so say what is wrong and
    name the two ways out.
    """
    built = [d for d in ("dist", "build", "out", ".output/public") if (root / d).is_dir()]
    built_hint = (
        (
            "<p>This project has a <code>"
            + "</code>, <code>".join(built)
            + "</code> folder — if that is a finished build, register THAT folder "
            "instead and it will preview from disk.</p>"
        )
        if built
        else ""
    )
    page_disp = _html.escape(page.name)
    entry_disp = _html.escape(entry)
    body = (
        f"<h3>{page_disp} needs a dev server</h3>"
        f"<p>Its only script is <code>{entry_disp}</code> — TypeScript/JSX, which "
        "the browser cannot run. A bundler has to transform it, so serving these "
        "files from disk renders an empty page.</p>"
        "<p><b>Start this project's dev server</b> (<code>npm run dev</code>), then "
        "press <b>Dev server</b> in the bar below the preview. Design Tweak will "
        "frame it directly, hot reload keeps working, and select-to-edit still "
        "works.</p>"
        f"{built_hint}"
    )
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<style>"
        "body{font:14px/1.6 system-ui,-apple-system,sans-serif;padding:28px 32px;"
        "color:#e6e6e6;background:#151517;max-width:56ch}"
        "h3{margin:0 0 12px;font-size:15px;font-weight:600}"
        "code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;"
        "background:#26262a;padding:1px 5px;border-radius:4px}"
        "p{margin:0 0 10px}b{color:#fff}"
        "</style>"
        f"{body}"
    ).encode("utf-8")


def _no_entry_response(
    root: Path, folder: Path, base: str, missing: str = ""
) -> tuple[int, str, bytes]:
    """404 page that explains WHY nothing rendered and offers what we found.

    Replaces a bare "Not found in project folder." — the common cause is a
    project with no top-level index.html (site lives in public/ or dist/, or
    the app is nested in a subfolder, or it isn't a static site at all).
    """
    try:
        # Re-contain both at entry so the scan/stat below consume the barrier's
        # return value regardless of what the caller passed in.
        root = _contained(root)
        folder = _contained(root, os.fspath(folder))
    except _PathEscape:
        return 403, "text/plain", b"forbidden"
    scan_from = folder if folder.is_dir() else root
    candidates = _scan_html(scan_from)
    if not candidates and scan_from != root:
        # An empty subfolder tells the user nothing — widen to the project root
        # so the suggestions are actually actionable.
        scan_from = root
        candidates = _scan_html(scan_from)
    try:
        prefix = scan_from.relative_to(root).as_posix()
    except ValueError:
        prefix = ""
    prefix = "" if prefix in ("", ".") else prefix + "/"

    if missing:
        head = f"<code>{_html.escape(missing)}</code> was not found in this project."
    else:
        head = f"No <code>index.html</code> in <code>{_html.escape(str(scan_from))}</code>."

    if candidates:
        links = "".join(
            f'<li><a href="{_html.escape(base + prefix + c)}">{_html.escape(prefix + c)}</a></li>'
            for c in candidates
        )
        body = (
            "<p>Design Tweak serves the folder as a static site and looks for an "
            "entry <code>index.html</code>. These HTML files are in the project — "
            "click one to preview it:</p>"
            f"<ul>{links}</ul>"
            "<p class='hint'>If the right entry point isn't listed, register the "
            "<em>subfolder</em> that contains it (<code>+ load new app</code>), or point "
            "Design Tweak at a running dev server URL for framework projects.</p>"
        )
    else:
        body = (
            "<p>No HTML files were found here, so there is nothing to serve "
            "statically. This usually means the project is a framework app "
            "(React / Vite / Next) that needs its dev server, or the previewable "
            "site lives in a subfolder that wasn't registered.</p>"
            "<p class='hint'>Fix it by either registering the subfolder that "
            "contains <code>index.html</code>, running the project's build "
            "(<code>npm run build</code>) and registering <code>dist/</code>, or "
            "starting <code>npm run dev</code> and pointing Design Tweak at "
            "<code>http://localhost:PORT</code>.</p>"
        )

    page = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>"
        "body{font:14px/1.6 system-ui,-apple-system,sans-serif;padding:28px 32px;"
        "color:#e6e6e6;background:#151517}"
        "h3{margin:0 0 12px;font-size:15px;font-weight:600}"
        "code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;"
        "background:#26262a;padding:1px 5px;border-radius:4px}"
        "ul{margin:12px 0;padding-left:20px}li{margin:3px 0}"
        "a{color:#7cc4ff}.hint{color:#9a9aa2;font-size:13px}"
        "</style>"
        f"<h3>{head}</h3>{body}"
    )
    return 404, "text/html; charset=utf-8", page.encode("utf-8")


# Names that must never be served out of a previewed project, even though they
# sit INSIDE the registered root and so pass containment. `is_sensitive_path`
# is HOME-relative and does not cover a project's own copy of any of these.
#
# Scoped deliberately to credential and VCS material, not to "files a site does
# not need": over-blocking a static preview turns into a support burden, and the
# threat being closed is same-origin read-back of a secret, not directory
# listing. `.git` is included because `.git/config` carries remote URLs that can
# embed a token, and every real dev server refuses it too.
_PROJECT_SECRET_NAMES: frozenset[str] = frozenset(
    {
        ".npmrc",
        ".netrc",
        ".pypirc",
        ".git-credentials",
        ".htpasswd",
        # Kiro Crew's per-app proxy-auth HMAC credential. Also covered by
        # `_is_kirocrew_internal` at its real home; listed here as well so a copy
        # that ends up inside a project tree is refused too.
        ".app_secret",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "secring.gpg",
    }
)
# Whole directories, matched on any path component. `.docker` holds
# `config.json`, whose `auths` entries are registry credentials.
_PROJECT_SECRET_DIRS: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", ".ssh", ".aws", ".gnupg", ".gpg", ".azure", ".kube", ".docker"}
)
# Private-key / keystore material by extension. `.key` is the conventional TLS
# private-key name (`server.key`); it is also Apple Keynote's extension, which is
# an acceptable false positive for a WEB preview — a Keynote document is not a
# servable web asset.
_PROJECT_SECRET_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p8",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)

# A PEM private-key header, matched against CONTENT rather than a filename.
#
# The extension list above is an enumeration, and an enumeration is only ever as
# good as the next filename someone picks (`privkey`, `id_deploy`, `server.key.bak`).
# This closes the class instead: whatever it is called, a file whose bytes open with
# a PEM private-key armour is refused. Cheap — the bytes are already in hand — and
# it only inspects the first line, so it cannot be fooled by a key mentioned later
# in an ordinary document.
_PEM_PRIVATE_KEY_RE = re.compile(rb"^-{5}BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-{5}")

# A PEM marker line on its own is a LABEL, not key material.
_PEM_MARKER_RE = re.compile(r"-{5}(?:BEGIN|END)[A-Z0-9 ]*PRIVATE KEY-{5}")
# 16+ base64-alphabet chars: enough to be an armoured key body rather than a word.
_PEM_BODY_RE = re.compile(r"[A-Za-z0-9+/=]{16,}")


def _contains_credential(data: bytes) -> bool:
    """True when *data* is text carrying a recognizable credential.

    Deliberately uses ONLY the labelled / vendor-prefixed pattern set from
    ``get_credential_patterns()`` — the same set deploy-web's pre-publish content
    scan reuses — and NOT the full ``redact_credentials()`` pipeline.

    That pipeline adds two further passes: a base64-chunk decode and an
    entropy-gated hunt for bare 40-char AWS secrets. ``security`` documents the
    latter as "the HIGHEST false-positive-risk redaction rule in the module", and
    both slide windows across every long base64-alphabet run. Running them per
    request over every served asset costs real CPU on the preview hot path, and a
    false positive here does not degrade quietly: the asset 403s and the user's
    preview renders blank or broken, on a surface whose entire job is faithful
    rendering. The labelled classes are the ones that actually appear checked into
    a project file, which is the reachable case.

    Binary assets (images, fonts, wasm) carry no scannable text; a strict UTF-8
    decode failure is the discriminator, and they are passed through.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    for pattern in get_credential_patterns():
        for match in pattern.finditer(text):
            hit = match.group()
            if "PRIVATE KEY" in hit:
                # The PEM branch also matches a header with no body, so prose that
                # quotes the marker — a docs page explaining key rotation — would
                # otherwise 403 and blank a legitimate page while leaking nothing.
                # Require actual armoured body before refusing. A file that IS a
                # key is caught by the first-line check regardless of its name.
                if not _PEM_BODY_RE.search(_PEM_MARKER_RE.sub("", hit)):
                    continue
            return True
    return False


def _is_project_secret(root: Path, target: Path) -> bool:
    """Whether *target* is credential material inside the previewed project.

    Matched on the path RELATIVE to *root* so the project's own `.env` is caught
    wherever the project happens to live. `target` is already the return value of
    `_contained(root, ...)`, so it is guaranteed to be inside *root* and this
    only has to classify, not re-validate.

    `.env` is matched as a PREFIX (`.env`, `.env.local`, `.env.production`)
    because the framework convention is a suffixed family, and blocking only the
    bare name would serve `.env.production` — the one most likely to hold a live
    key.
    """
    try:
        rel_parts = target.relative_to(root).parts
    except ValueError:  # pragma: no cover — `target` came from `_contained(root)`
        return True  # unrelatable to the root: refuse rather than guess
    for part in rel_parts:
        low = part.lower()
        if low in _PROJECT_SECRET_DIRS or low in _PROJECT_SECRET_NAMES:
            return True
        # Any dotfile whose name starts `.env` — `.env`, `.env.local`,
        # `.env.production`, and direnv's `.envrc` / `.envrc.local`. Matched as a
        # bare prefix rather than `.env` + `.env.`: requiring the dot let
        # `.envrc` through, and `.envrc` routinely holds `export AWS_SECRET…`,
        # which the PEM content backstop below cannot recognise because it is
        # shell, not armoured key material. A non-secret dotfile caught by this
        # (`.env.example`) is not a servable web asset, so the over-match costs
        # nothing.
        if low.startswith(".env"):
            return True
        if low.endswith(_PROJECT_SECRET_SUFFIXES):
            return True
    return False


def _static_response(
    root_str: str, rel: str, base: str, script: str = INJECT_PUBLIC
) -> tuple[int, str, bytes]:
    """Serve one file from a project folder, contained and overlay-injected.

    `base` is the public URL prefix the served folder is reachable at — it
    becomes the `<base href>` for an entry that lives in a subdirectory, and it
    prefixes the links on the diagnostic 404. `script` is the overlay's URL from
    the FRAMED page's point of view, which differs per server.
    """
    rel = rel.lstrip("/")
    try:
        # ONE barrier for both values: `_contained(root_str)` normalises the
        # project root, `_contained(root, rel)` proves the requested file is
        # inside it. Everything below consumes these RETURN values, so no
        # request-supplied path reaches a stat or a read.
        root = _contained(root_str)
        target = _contained(root, rel)
    except _PathEscape:
        return 403, "text/plain", b"forbidden"
    # Containment is NOT sufficient: `_valid_root()` screens only the REGISTERED
    # folder, so a root that is an ancestor of a credential directory (registering
    # `~` is a natural pick when a site lives at `~/index.html`) leaves every
    # secret under it "contained" and therefore servable. This server is
    # deliberately unauthenticated, so screen the REQUESTED path too — the
    # sibling `file_explorer` builtin checks the requested path for the same
    # reason, not just its allowed roots.
    if is_sensitive_path(str(target)):
        return 403, "text/plain", b"forbidden"
    # `is_sensitive_path` is HOME-relative: it covers `~/.aws`, `~/.npmrc` and
    # Kiro Crew's own data home, but NOT a credential file sitting inside the
    # previewed project itself. A web project's `.env` is exactly that, and it is
    # the common case rather than a contrived one -- `create-vite` writes one, and
    # it routinely holds a real API key.
    #
    # This matters because a real dev server REFUSES to serve `.env` while this
    # static server would hand over any contained byte. The preview is same-origin
    # with the project's own scripts, so `fetch('/.env')` from the page (or from
    # any third-party script it loads) would read it back. Screen the project-
    # relative path too.
    if _is_project_secret(root, target):
        return 403, "text/plain", b"forbidden"
    # Kiro Crew's own trees are refused outright, whatever the project root is.
    # This is the barrier that actually holds: the checks above are a HOME-relative
    # leaf list and a project-relative name list, and neither knows about
    # `<crew home>/apps/<app>/.app_secret` or `<crew home>/history/*.jsonl`.
    if _is_kirocrew_internal(target):
        return 403, "text/plain", b"forbidden"
    if target.is_dir():
        # Pass the project ROOT: `_find_entry` screens each candidate with
        # `_is_project_secret`, which is relative to the project, not to whatever
        # subdirectory was requested.
        entry = _find_entry(target, root)
        if entry is None:
            return _no_entry_response(root, target, base)
        target = entry
    if not target.is_file():
        return _no_entry_response(root, target, base, missing=rel)
    # Size-check BEFORE reading. This server buffers the whole file (there is no
    # streaming path), so a large asset in the previewed project — a video, a
    # design source file, an archive — would be materialised in memory in one go
    # and can take the backend process down. The project is the user's own, so
    # this is an accident waiting to happen rather than an attack; either way the
    # ceiling is what keeps a preview from killing the app.
    try:
        size = target.stat().st_size
    except OSError as exc:
        return 500, "text/plain", str(exc).encode()
    if size > MAX_STATIC_BYTES:
        msg = f"file is {size} bytes, over the {MAX_STATIC_BYTES}-byte preview limit"
        return 413, "text/plain", msg.encode()
    # The size check above is advisory — it stats a NAME, so it cannot bind the
    # bytes that get read. `_contained` proved an ancestor at walk time, and
    # `O_NOFOLLOW` alone would only guard the final component, so a nested
    # directory swapped for a symlink between the walk and the open would escape
    # the approved tree. This helper opens first and validates the DESCRIPTOR's
    # real path against the root, so the inode checked is the inode served, and
    # it re-applies the byte ceiling as the authority rather than the hint.
    data = safe_read_file_bytes_nolink(
        str(target), within_root=str(root), max_bytes=MAX_STATIC_BYTES
    )
    if data is None:
        return 403, "text/plain", b"forbidden"    # Content-based backstop for the private-key class: refuse whatever the file is
    # named if its bytes actually open with PEM private-key armour. The filename
    # lists above cannot enumerate every name a project might use.
    if _PEM_PRIVATE_KEY_RE.match(data.lstrip()[:64]):
        return 403, "text/plain", b"forbidden"
    # Same reasoning, widened past private keys: a checked-in credential can sit in
    # any ordinary project file (`config.js`, a JSON fixture, a committed sample),
    # and the previewed page's OWN JavaScript is same-origin with this server, so it
    # can fetch any file under the root — a pasted third-party prototype included.
    # A name-based list cannot cover that, so gate on content.
    if _contains_credential(data):
        return 403, "text/plain", b"forbidden"
    ctype = _guess_ctype(target)
    if "text/html" in ctype:
        # A bundler template cannot render statically — bail out with an
        # explanation rather than a page guaranteed to come up blank.
        entry = _unbundled_entry(data)
        if entry:
            # 200, not an error: the file was found and read fine. The page IS
            # the answer, and a 4xx here would trip the panel's error handling.
            return 200, "text/html; charset=utf-8", _needs_dev_server_body(root, target, entry)
        # <base> must point at the SERVED FILE's own directory, not the project
        # root — otherwise an index.html living in public/ or app/ resolves its
        # relative assets one level too high and renders blank.
        try:
            sub_dir = target.parent.relative_to(root).as_posix()
        except ValueError:
            sub_dir = "."
        html_base = base if sub_dir in ("", ".") else base + sub_dir + "/"
        return (
            200,
            "text/html; charset=utf-8",
            _rewrite_html(data, html_base, script=script),
        )
    return 200, ctype, data


class Handler(BaseHTTPRequestHandler):
    server_version = "KiroCrew-SelectToEdit/" + VERSION  # brand-ok: Server-header token
    timeout = _CLIENT_READ_TIMEOUT  # see `_CLIENT_READ_TIMEOUT`

    def log_message(self, *args) -> None:  # silence default logging
        pass

    # ---- routing ----
    def _route(self) -> tuple[str, dict]:
        url = urlparse(self.path)
        route = url.path.rstrip("/") or "/"
        if route.startswith("/api/"):
            route = route[4:] or "/"
        elif route == "/api":
            route = "/"
        return route, parse_qs(url.query)

    def _read_raw_body(self) -> bytes:
        """Read the request body once, enforcing the size cap.

        The bytes are cached on the handler so the HMAC gate and the JSON
        parser (`_read_body`) both see the same payload without re-reading the
        socket.
        """
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ValueError("payload too large")
        # A short read means the client promised bytes it never sent (or stalled
        # past `timeout`). Treat the request as unframeable rather than acting on
        # a truncated payload, and close: the missing bytes would otherwise be
        # read as the head of the next request on a keep-alive connection.
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            raise _IncompleteBody("request body shorter than Content-Length")
        return body

    def _authorized(self, method: str, body: bytes) -> bool:
        """Verify the gateway's X-KiroCrew-Proxy HMAC before dispatch (CWE-306).

        `/health` (and its aliases) stay unauthenticated because the gateway's
        own liveness probe hits the backend directly, unsigned.
        """
        route = urlparse(self.path).path.rstrip("/")
        if route in ("", "/health", "/api", "/api/health"):
            return True
        if verify_proxy_request(
            self.headers.get("X-KiroCrew-Proxy", ""),
            method=method,
            target=self.path,
            body=body,
        ):
            return True
        # Audit the denial before answering, following the file-explorer and
        # md-notebook builtins' `proxy_auth_failed` convention — an unsigned
        # local process probing this backend is exactly what the SEL trail
        # exists to record, and a permission denial that leaves no record is
        # indistinguishable from one that never happened.
        #
        # Called directly rather than off-loop: this is a ThreadingHTTPServer,
        # so the handler already owns a worker thread and there is no event loop
        # for a blocking SEL write to stall (md-notebook needs
        # `asyncio.to_thread` only because it runs on aiohttp).
        #
        # Log the path WITHOUT the query string — it can carry a project path.
        sel().log_api_access(
            caller=APP_NAME,
            operation="proxy_auth_failed",
            outcome="denied",
            source="builtin-app",
            resources=urlparse(self.path).path,
        )
        self._json(
            401,
            {"error": "invalid or missing proxy signature", "code": "invalid_proxy_signature"},
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized("GET", b""):
            return
        try:
            route, qs = self._route()
            if route in ("/", "/health"):
                return self._json(
                    200,
                    {
                        "status": "ok",
                        "app": APP_NAME,
                        "version": VERSION,
                        "pending": len(_pending_files()),
                        "dataDir": str(DATA_DIR),
                    },
                )
            if route == "/queue":
                # Requests, oldest first — each carries its comments as sub-items.
                pending = []
                for fp in _pending_files():
                    req = _read_request(fp)
                    if req is not None:
                        pending.append(_summarize(req))
                pending.sort(key=lambda r: r.get("number") or 0)
                return self._json(200, {"pending": pending})
            if route == "/latest":
                # The newest request, full payload — what the agent reads to work
                # a batch it was just handed.
                files = _pending_files()
                if not files:
                    return self._json(200, {})
                newest, newest_num = None, -1
                for fp in files:
                    req = _read_request(fp)
                    if req and (req.get("number") or 0) > newest_num:
                        newest, newest_num = req, req.get("number") or 0
                # This is the one read path that hands the request's raw dict
                # straight to a caller rather than routing through _summarize()'s
                # panel-facing shape, so it must apply the SAME redaction floor
                # _summarize()/_summarize_comment() apply on their own thread
                # fields — otherwise agent-written credential text in the queue
                # JSON reaches this authenticated GET verbatim. Every other field
                # is passed through unchanged: the agent needs the full payload
                # (selection elements, locators, source hints), only the request
                # and comment threads carry free-text an agent could have pasted
                # a secret into.
                if newest is not None:
                    newest = {
                        **newest,
                        "thread": _redact_thread(newest.get("thread")),
                        "comments": [
                            {**c, "thread": _redact_thread(c.get("thread"))}
                            for c in (newest.get("comments") or [])
                        ],
                    }
                return self._json(200, newest or {})
            if route == "/projects":
                return self._h_projects_list()
            if route == "/detect-dev-server":
                return self._h_detect_dev_server(qs)
            if route == "/history":
                done = []
                for fp in sorted(HANDLED_DIR.glob("*.json"), reverse=True)[:50]:
                    req = _read_request(fp)
                    if req is not None:
                        done.append(_summarize(req))
                return self._json(200, {"history": done})
            if route == "/proxy-inject.js":
                return self._h_inject()
            if route == "/proxy" or route.startswith("/proxy/"):
                # REMOVED DELIBERATELY — do not restore. See the note above
                # `_static_preview_base`. This route served project-controlled
                # content from the DASHBOARD's origin, and the preview iframe runs
                # with `allow-same-origin`. A hostile previewed page could read the
                # dashboard origin off `document.referrer` and navigate itself
                # here; because the document it then loaded was its OWN html on
                # our origin, its script ran first-party and could reach the
                # authenticated API and the parent DOM. Navigating to any OTHER
                # dashboard URL gains nothing (navigation replaces the document
                # with ours), so THIS route was the whole bridge.
                return self._json(
                    410,
                    {
                        "error": "the dashboard-origin preview route was removed for "
                        "security; previews are served from an ephemeral loopback "
                        "server instead (see /projects → previewUrl)"
                    },
                )
            return self._json(404, {"error": f"GET {route} not found"})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_raw_body()
        except _IncompleteBody as exc:
            return self._json(400, {"error": str(exc), "code": "incomplete_body"})
        except ValueError as exc:
            return self._json(413, {"error": str(exc)})
        except OSError:
            # Read timed out (see `_CLIENT_READ_TIMEOUT`). The socket state is
            # indeterminate, so close instead of framing another request on it.
            self.close_connection = True
            return self._json(408, {"error": "request body timed out", "code": "body_timeout"})
        if not self._authorized("POST", body):
            return
        self._cached_body = body
        try:
            route, qs = self._route()
            if route == "/submit":
                return self._h_submit()
            if route == "/clear":
                return self._h_clear(qs)
            if route == "/delete":
                return self._h_delete(qs)
            if route in ("/source", "/target"):
                return self._h_set_source()
            if route == "/projects":
                return self._h_projects_add()
            if route == "/projects/select":
                return self._h_projects_select()
            if route == "/projects/remove":
                return self._h_projects_remove()
            if route == "/projects/preview-url":
                return self._h_projects_preview_url()
            if route == "/dev-server/start":
                return self._h_dev_server_start(qs)
            if route == "/dev-server/stop":
                return self._h_dev_server_stop(qs)
            if route == "/pick-folder":
                return self._h_pick_folder()
            if route == "/send":
                return self._h_send(qs)
            if route == "/delivered":
                return self._h_delivered(qs)
            if route == "/delete-comment":
                return self._h_delete_comment(qs)
            if route == "/thread":
                return self._h_thread(qs)
            return self._json(404, {"error": f"POST {route} not found"})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": str(exc)})

    # ---- handlers ----
    def _read_body(self) -> dict:
        # The raw bytes were already read (and size-capped) by _read_raw_body in
        # do_POST, then HMAC-verified. Parse that cached copy.
        raw = getattr(self, "_cached_body", b"")
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("payload must be a JSON object")
        return data

    def _h_submit(self) -> None:
        """Append a captured comment to the project's OPEN DRAFT request.

        Creates the draft if there isn't one. Never dispatches — sending is an
        explicit, separate step (POST /send) so a batch of comments goes to the
        agent as a single request.
        """
        payload = self._read_body()
        if payload.get("type") != "visual_edit_request":
            return self._json(400, {"error": "type must be 'visual_edit_request'"})
        sel = payload.get("selection") or {}
        elements = sel.get("elements") if isinstance(sel, dict) else None
        # Every element must be a DICT, not merely present. `_el_name` does
        # `el.get("tag")`, so a string element raises AttributeError when
        # `/queue` summarises the request — and because the bad selection was
        # already persisted by then, that endpoint keeps returning 500 on every
        # later poll until someone deletes the queue file by hand. The preview
        # page controls this payload, so it is a one-request self-inflicted
        # outage. Reject it at the boundary instead.
        if not isinstance(elements, list) or not elements:
            return self._json(
                400,
                {"error": "selection.elements is required", "code": "selection_required"},
            )
        if not all(isinstance(el, dict) for el in elements):
            return self._json(
                400,
                {
                    "error": "selection.elements must contain only objects",
                    "code": "selection_malformed",
                },
            )
        # A dict is not enough: the FIELD TYPES are what `_el_name` consumes. A
        # non-string `tag` (`{"tag": 42, "id": "x"}`) used to raise `TypeError`
        # on `str + str`, and `classes` holding non-strings broke the `.join`.
        # Same outage shape as above and for the same reason -- the record is
        # persisted before anything reads it back -- so it is refused here, where
        # the caller still gets an error it can act on. `_el_name` is separately
        # total, which contains records this guard never saw (an older build's
        # queue file); this check is what makes a malformed submit visible rather
        # than silently relabelled.
        for el in elements:
            if not all(
                isinstance(el[k], str) for k in ("tag", "id") if el.get(k) is not None
            ):
                return self._json(
                    400,
                    {
                        "error": "selection.elements[].tag and .id must be strings",
                        "code": "selection_malformed",
                    },
                )
            classes = el.get("classes")
            if classes is not None and (
                not isinstance(classes, list)
                or not all(isinstance(c, str) for c in classes)
            ):
                return self._json(
                    400,
                    {
                        "error": "selection.elements[].classes must be a list of strings",
                        "code": "selection_malformed",
                    },
                )

        preview_url = str(payload.get("previewUrl", ""))
        project_id, project_root, source_file = _resolve_project(payload)
        # The selection is persisted verbatim below and its per-element `source`
        # block is handed to the agent as an edit target, so contain those paths
        # before they are stored — same barrier `sourceFile` goes through.
        _sanitize_selection_sources(sel, project_root)

        def _txn() -> tuple[int, dict]:
            # Find-or-open the draft, number it, append, write — ONE transaction.
            # Splitting it would let two tabs each see "no open draft" and both
            # create a request with the same number, or (the reported bug) each
            # append to its own copy of the same draft and lose one comment.
            fp = _open_draft_file(project_id)
            if fp is None:
                rid = _new_id()
                req: dict[str, Any] = {
                    "type": "visual_edit_batch",
                    "id": rid,
                    "number": _next_number(project_id),
                    "state": "draft",
                    "projectId": project_id,
                    "projectRoot": project_root,
                    "createdAt": _now_iso(),
                    "sentAt": "",
                    "thread": [],
                    "comments": [],
                }
                fp = _request_file(QUEUE_DIR, rid)
            else:
                existing = _read_request(fp)
                if existing is None:
                    return 500, {"error": "draft request unreadable"}
                req = existing

            comments: list[dict[str, Any]] = req.setdefault("comments", [])
            # A capture originates from a gesture INSIDE the previewed page, so a
            # hostile project could post them in a loop. The draft is reviewable
            # and nothing reaches the agent without a separate Send, so the real
            # exposure is unbounded growth of the user's own queue file rather
            # than an agent action. Cap the draft instead of demanding a
            # parent-side gesture, which is the interaction this product IS.
            if len(comments) >= MAX_DRAFT_COMMENTS:
                return 429, {
                    "error": (
                        f"this request already holds {MAX_DRAFT_COMMENTS} comments — "
                        "send or clear it before adding more"
                    ),
                    "code": "draft_comment_limit",
                }
            # Always minted here, never taken from the payload. The id is the
            # lookup key for `/delete-comment` and `/thread`, and both read it
            # from the query string — i.e. always as a `str`. A caller-supplied
            # `cid` could arrive as a JSON number, pass a `str()`-ed format check,
            # and persist as an int that neither lookup can ever match again,
            # leaving a comment that cannot be deleted or replied to. A duplicate
            # of an existing id has the mirror-image problem: one delete would
            # take both comments. The response below hands the id back, so no
            # caller needs to choose one.
            cid = _new_id()
            created = payload.get("createdAt") or _now_iso()

            # A follow-up references an earlier comment (possibly in an already-sent
            # request); it still ships in THIS batch, just linked to its origin.
            follow_up_to = str(payload.get("followUpTo", "") or "")
            if follow_up_to and not _ID_RE.match(follow_up_to):
                follow_up_to = ""

            comment = {
                "cid": cid,
                "index": len(comments) + 1,
                "status": "new",
                "comment": str(payload.get("comment", "")),
                "createdAt": created,
                "selection": sel,
                "previewUrl": preview_url,
                # Stored per comment as well as on the request: the panel matches
                # pins to the previewed project by this id, and matching on the id
                # (rather than on the shape of previewUrl) is what lets a project
                # previewed straight from its dev server keep its pins.
                "projectId": project_id,
                "sourceFile": source_file,
                "followUpTo": follow_up_to,
                # The user's own comment seeds the thread so the in-preview bubble
                # reads as a conversation from the first frame.
                "thread": [
                    {"role": "user", "text": str(payload.get("comment", "")), "ts": created}
                ],
            }
            if _TARGET and not project_root:
                comment["devServer"] = _TARGET
            comments.append(comment)
            _write_request(fp, req)

            return 200, {
                "ok": True,
                "id": req["id"],
                "number": req["number"],
                "state": req["state"],
                "cid": cid,
                "index": comment["index"],
                "label": f"{req['number']}.{comment['index']}",
                "commentCount": len(comments),
                "savedTo": str(fp),
            }

        with _QUEUE_LOCK:
            try:
                code, body = _txn()
            except _RecordTooLarge as exc:
                # The append was refused and the prior record is intact, so this
                # is a full draft rather than lost work — say which.
                code, body = 413, {"error": str(exc), "code": "record_too_large"}
        return self._json(code, body)

    def _h_send(self, qs: dict) -> None:
        """Seal a draft request and mark every comment as sent (seal-on-send).

        After this the request never accepts new comments, so the next captured
        comment opens a fresh draft even while this batch is still in flight.
        """
        rid = (qs.get("id") or [""])[0]
        if not rid or not _ID_RE.match(rid):
            return self._json(400, {"error": "valid id required"})
        fp = _request_file(QUEUE_DIR, rid)

        def _txn() -> tuple[int, dict]:
            if not fp.is_file():
                return 404, {"error": "not found"}
            req = _read_request(fp)
            if req is None:
                return 500, {"error": "request unreadable"}
            comments = req.get("comments") or []
            if not comments:
                return 400, {"error": "request has no comments"}
            if not _is_draft(req):
                return 200, {"ok": True, "already": True, "request": _summarize(req)}
            req["state"] = "sent"
            req["sentAt"] = _now_iso()
            for c in comments:
                if c.get("status") == "new":
                    c["status"] = "sent"
            _write_request(fp, req)
            return 200, {"ok": True, "request": _summarize(req)}

        with _QUEUE_LOCK:
            try:
                code, body = _txn()
            except _RecordTooLarge as exc:
                # The append was refused and the prior record is intact, so this
                # is a full draft rather than lost work — say which.
                code, body = 413, {"error": str(exc), "code": "record_too_large"}
        return self._json(code, body)

    def _h_delivered(self, qs: dict) -> None:
        """Acknowledge that a sealed request's prompt actually reached the agent.

        `/send` seals the batch server-side, but the prompt is dispatched by the
        panel afterwards. Those are two steps, so a tab closed (or crashed)
        between them left the request sealed with nothing in flight: the send bar
        is gone once a request is no longer a draft, so the work was stranded
        with no way to retry it. Sealing after dispatch instead would reopen the
        window where a second tab joins the draft mid-prompt, so the fix is a
        separate acknowledgement rather than a reorder.

        Idempotent: re-acknowledging keeps the FIRST timestamp, so a duplicate
        call cannot make a delivered request look newer than it is.
        """
        rid = (qs.get("id") or [""])[0]
        if not rid or not _ID_RE.match(rid):
            return self._json(400, {"error": "valid id required", "code": "id_required"})
        fp = _request_file(QUEUE_DIR, rid)

        def _txn() -> tuple[int, dict]:
            if not fp.is_file():
                return 404, {"error": "not found", "code": "not_found"}
            req = _read_request(fp)
            if req is None:
                return 500, {"error": "request unreadable", "code": "unreadable"}
            # A draft was never dispatched, so acknowledging one would mark work
            # delivered that no agent has seen — and would suppress the retry bar
            # for the very state it is meant to cover.
            if _is_draft(req):
                return 409, {
                    "error": "request is still a draft — nothing was dispatched",
                    "code": "not_sealed",
                }
            if not req.get("deliveredAt"):
                req["deliveredAt"] = _now_iso()
                _write_request(fp, req)
            return 200, {"ok": True, "request": _summarize(req)}

        with _QUEUE_LOCK:
            try:
                code, body = _txn()
            except _RecordTooLarge as exc:
                # The append was refused and the prior record is intact, so this
                # is a full draft rather than lost work — say which.
                code, body = 413, {"error": str(exc), "code": "record_too_large"}
        return self._json(code, body)

    def _h_delete_comment(self, qs: dict) -> None:
        """Drop a single comment from a DRAFT request (undo a mis-click).

        Refused once the request is sent — the agent already has that batch.
        """
        rid = (qs.get("id") or [""])[0]
        cid = (qs.get("cid") or [""])[0]
        if not rid or not _ID_RE.match(rid) or not cid or not _ID_RE.match(cid):
            return self._json(400, {"error": "valid id and cid required"})
        fp = _request_file(QUEUE_DIR, rid)

        def _txn() -> tuple[int, dict]:
            if not fp.is_file():
                return 404, {"error": "not found"}
            req = _read_request(fp)
            if req is None:
                return 500, {"error": "request unreadable"}
            if not _is_draft(req):
                return 409, {"error": "request already sent — cannot remove comments"}
            comments = req.get("comments") or []
            kept = [c for c in comments if c.get("cid") != cid]
            if len(kept) == len(comments):
                return 404, {"error": "comment not found"}
            for n, c in enumerate(kept, start=1):
                c["index"] = n  # keep sub-numbering contiguous (3.1, 3.2, …)
            req["comments"] = kept
            # An emptied draft is noise in the rail — drop the request with it.
            if not kept:
                try:
                    fp.unlink()
                except OSError as exc:
                    return 500, {"error": str(exc)}
                return 200, {"ok": True, "id": rid, "removedRequest": True}
            _write_request(fp, req)
            return 200, {"ok": True, "id": rid, "cid": cid, "request": _summarize(req)}

        with _QUEUE_LOCK:
            try:
                code, body = _txn()
            except _RecordTooLarge as exc:
                # The append was refused and the prior record is intact, so this
                # is a full draft rather than lost work — say which.
                code, body = 413, {"error": str(exc), "code": "record_too_large"}
        return self._json(code, body)

    def _h_clear(self, qs: dict) -> None:
        rid = (qs.get("id") or [""])[0]
        if not rid or not _ID_RE.match(rid):
            return self._json(400, {"error": "valid id required"})
        src = _request_file(QUEUE_DIR, rid)
        dst = _request_file(HANDLED_DIR, rid)

        def _txn() -> tuple[int, dict]:
            # Archiving is a rename, not a read-modify-write, but it still has to
            # be serialized: a `/thread` append that read this path before the
            # move would otherwise write the file back into queue/, resurrecting
            # a request the user just cleared.
            if not src.exists():
                return 404, {"error": "not found"}
            try:
                src.replace(dst)
            except OSError as exc:
                return 500, {"error": str(exc)}
            return 200, {"ok": True, "id": rid}

        with _QUEUE_LOCK:
            try:
                code, body = _txn()
            except _RecordTooLarge as exc:
                # The append was refused and the prior record is intact, so this
                # is a full draft rather than lost work — say which.
                code, body = 413, {"error": str(exc), "code": "record_too_large"}
        return self._json(code, body)

    def _h_delete(self, qs: dict) -> None:
        """Permanently delete a request (from queue/ or handled/). Unlike /clear
        (which archives to handled/), this removes the file entirely."""
        rid = (qs.get("id") or [""])[0]
        if not rid or not _ID_RE.match(rid):
            return self._json(400, {"error": "valid id required"})

        def _txn() -> tuple[int, dict] | None:
            # Same serialization reason as `_h_clear`: without the lock a
            # concurrent write can recreate the file right after the unlink.
            removed = False
            for d in (QUEUE_DIR, HANDLED_DIR):
                fp = _request_file(d, rid)
                if fp.exists():
                    try:
                        fp.unlink()
                        removed = True
                    except OSError as exc:
                        return 500, {"error": str(exc)}
            if not removed:
                return 404, {"error": "not found"}
            return None  # deleted — caller emits the shared success response

        with _QUEUE_LOCK:
            failed = _txn()
        if failed is not None:
            return self._json(*failed)
        return self._json(200, {"ok": True, "id": rid, "deleted": True})

    # ---- project registry handlers ----
    def _h_projects_list(self) -> None:
        active = _active_project()
        serving = bool(_ROOT and active and str(Path(active["path"]).resolve()) == _ROOT)
        # Classification is computed per request, never stored: a folder becomes
        # static the moment its build lands in dist/, and a stale flag would keep
        # showing "needs a dev server" for something that now previews fine.
        out = []
        for p in _CFG["projects"]:
            row = dict(p)
            root = _valid_root(p["path"])
            row.update(
                _classify_project(root)
                if root
                else {
                    "needsDevServer": False,
                    "devCommand": "",
                    "unbundledEntry": "",
                    "hasEntry": False,
                }
            )
            row["devRunning"] = _dev_proc_alive(p["id"])
            # The injecting proxy's port is ephemeral: it lives and dies with this
            # backend, so it is resolved live here rather than persisted. A saved
            # port would be guaranteed dead after a restart.
            live = _DEV_PROCS.get(p["id"]) or {}
            if live.get("proxyUrl"):
                row["previewUrl"] = live["proxyUrl"]
                row["devUrl"] = live.get("url", "")
            elif _valid_target(str(row.get("previewUrl") or "").strip()):
                # A PERSISTED dev URL has to be framed through the injecting proxy
                # too. Returned bare it still renders — which is exactly why this
                # was invisible — but the proxy is the only thing that injects the
                # select-to-edit overlay, so the feature silently does nothing on
                # precisely the framework projects that need a dev server. Matches
                # what `_start_dev_proc` and the adopt path in
                # `_h_dev_server_start` already do for a freshly-started server.
                #
                # `_front_with_proxy` reuses this project's live proxy, so polling
                # this endpoint does not spawn a listener per request, and it
                # returns the bare URL unchanged if the proxy cannot bind.
                #
                # The loopback allow-list is re-asserted here rather than trusted
                # from `_h_projects_add` / `_h_projects_preview_url`: this value is
                # read back off disk and is about to become a proxy upstream. A
                # value that FAILS it is cleared in the `else` below — never handed
                # back bare.
                dev_url = str(row["previewUrl"]).strip()
                row["previewUrl"] = _front_with_proxy(p["id"], dev_url)
                row["devUrl"] = dev_url
            elif root is not None and not row.get("previewUrl"):
                # Static preview: framed from the app's OWN loopback server, never
                # from the dashboard origin (see `_StaticInjectHandler`). Same
                # ephemeral-by-design reasoning as the dev proxy above, so it is
                # resolved live here too. If it cannot bind we leave previewUrl
                # empty and the panel falls back to the gateway-proxied route.
                static_url = _static_preview_url(p["id"])
                if static_url:
                    row["previewUrl"] = static_url
                    row["previewMode"] = "static"
            elif row.get("previewUrl"):
                # A NON-EMPTY previewUrl that failed `_valid_target` reaches here:
                # it is not live-proxied, not re-provable as a loopback dev URL,
                # and the static branch above only fires when the value is empty.
                # Handing it back would frame it DIRECTLY — bypassing the proxy
                # that strips `Cookie`/`Authorization` — so the dashboard's
                # host-scoped session cookie would reach whatever it names. Clear
                # it and let the panel render the unreachable state, matching what
                # `_front_with_proxy` does when the proxy cannot bind.
                #
                # Reachable from a value persisted by an older build, or one whose
                # host/scheme stopped qualifying (e.g. `https://127.0.0.1:5173` —
                # the allow-list is http-only), so validating only on write is not
                # enough.
                row["previewUrl"] = ""
            out.append(row)
        return self._json(
            200,
            {
                "projects": out,
                "activeId": _CFG["activeId"],
                "serving": serving,
                "version": VERSION,
            },
        )

    def _h_dev_server_start(self, qs: dict) -> None:
        """Start the project's own dev server and point the preview at it."""
        pid = (qs.get("id") or [""])[0]
        proj = next((p for p in _CFG["projects"] if p["id"] == pid), None)
        if proj is None:
            return self._json(404, {"error": "project not found"})
        root = _valid_root(proj["path"])
        if root is None:
            return self._json(400, {"error": f"folder no longer readable: {proj['path']}"})

        # Already running elsewhere (started by hand in a terminal)? Adopt it
        # rather than starting a second one on another port.
        adopted = _auto_dev_server(root)
        if adopted:
            # Front it with the injecting proxy as well — a server the user started
            # serves its own HTML, so without this the overlay never loads and
            # select-to-edit is missing on exactly the projects that need it most.
            framed = _front_with_proxy(pid, adopted)
            # NOT persisted: the proxy port dies with this backend, so a saved URL
            # is guaranteed dead after a restart. _h_projects_list resolves the live
            # one per request instead.
            return self._json(
                200,
                {
                    "ok": True,
                    "url": framed,
                    "devUrl": adopted,
                    "adopted": True,
                    "injected": bool(framed) and framed != adopted,
                    "project": proj,
                },
            )

        res = _start_dev_proc(pid, root)
        if not res.get("ok"):
            return self._json(200, res)  # 200: the error text IS the answer
        # Likewise not persisted — see above.
        return self._json(200, {**res, "project": proj})

    def _h_dev_server_stop(self, qs: dict) -> None:
        """Stop a dev server WE started and revert the preview to serving from disk.

        A server the user started themselves is left running — Design Tweak did not
        start it, so killing it would be a surprise. Its URL is just forgotten.
        """
        pid = (qs.get("id") or [""])[0]
        proj = next((p for p in _CFG["projects"] if p["id"] == pid), None)
        if proj is None:
            return self._json(404, {"error": "project not found"})
        # `_stop_dev_proc` stays OUTSIDE the lock ON PURPOSE: it escalates
        # SIGTERM -> SIGKILL and WAITS on the child, so holding `_QUEUE_LOCK`
        # across it would stall every queue operation for the length of that
        # teardown.
        stopped = _stop_dev_proc(pid)
        # The registry write, however, has to be serialized like every other one
        # in this file. `_save_cfg` is a whole-file atomic replace, so two
        # concurrent replacements do not interleave -- the loser is simply
        # overwritten, and a project added or removed in the racing request
        # disappears on the next load. This was the only `_save_cfg(_CFG)` call
        # site not under the lock.
        with _QUEUE_LOCK:  # read-modify-write over the shared registry
            # Re-resolve inside the lock. `proj` was found before the teardown,
            # which is a long window: a concurrent remove may have dropped it,
            # and mutating that detached dict would save a registry the project
            # is no longer part of.
            live = next((p for p in _CFG["projects"] if p["id"] == pid), None)
            if live is not None:
                live.pop("previewUrl", None)
                _save_cfg(_CFG)
                proj = live
        return self._json(200, {"ok": True, "stopped": stopped, "project": proj})

    def _h_projects_add(self) -> None:
        data = self._read_body()
        raw = str(data.get("path", "")).strip()
        root = _valid_root(raw)
        if root is None:
            return self._json(400, {"error": f"not a readable folder: {raw}"})
        # Optional dev-server URL. A project is always identified by its FOLDER —
        # that is where the agent edits — and the URL only changes how it is
        # previewed: framed directly instead of proxied from disk. Framework
        # projects need that because this backend cannot proxy a WebSocket, so
        # HMR dies behind the proxy.
        preview_url = str(data.get("previewUrl", "") or "").strip().rstrip("/")
        if preview_url and not _valid_target(preview_url):
            return self._json(
                400,
                {
                    "error": "dev server URL must be http://localhost:PORT or http://127.0.0.1:PORT",
                },
            )

        def _existing() -> dict | None:
            for p in _CFG["projects"]:
                if str(Path(p["path"]).resolve()) == str(root):
                    return p
            return None

        # Scan-then-append is a read-modify-write over the shared registry, and
        # this is a ThreadingHTTPServer: without the lock two concurrent adds of
        # the same folder both miss the dedupe and both append it.
        with _QUEUE_LOCK:
            p = _existing()
            if p is not None:
                if preview_url and p.get("previewUrl", "") != preview_url:
                    p["previewUrl"] = preview_url
                    _save_cfg(_CFG)
                    return self._json(
                        200, {"ok": True, "project": p, "existing": True, "updated": "previewUrl"}
                    )
                return self._json(200, {"ok": True, "project": p, "existing": True})

        proj = {"id": uuid.uuid4().hex[:8], "path": str(root), "name": root.name}

        # No URL typed? Look for a dev server already serving this folder. Only
        # an UNAMBIGUOUS match is attached (exactly one candidate serving HTML) —
        # with several running, silently picking one would point the preview at
        # the wrong app, so they are returned for the user to choose instead.
        #
        # Deliberately OUTSIDE the lock: `_detect_dev_servers` shells out to
        # `lsof`, and holding the registry lock across that would serialize every
        # queue transaction behind a subprocess. The re-check below closes the
        # window this opens.
        detected = []
        if preview_url:
            proj["previewUrl"] = preview_url
        else:
            detected = _detect_dev_servers(root)
            auto = [c for c in detected if c["servesHtml"]]
            if len(auto) == 1:
                proj["previewUrl"] = auto[0]["url"]

        with _QUEUE_LOCK:
            # Re-check: another request may have registered this same folder while
            # detection was running above.
            p = _existing()
            if p is not None:
                return self._json(200, {"ok": True, "project": p, "existing": True})
            _CFG["projects"].append(proj)
            _save_cfg(_CFG)
        return self._json(
            200,
            {
                "ok": True,
                "project": proj,
                "detected": detected,
                "autoDetected": bool(not preview_url and proj.get("previewUrl")),
            },
        )

    def _h_detect_dev_server(self, qs: dict) -> None:
        """Dev servers plausibly serving a project — for the UI's Detect button.

        Takes either `?id=<projectId>` or `?path=<folder>` so it works before a
        project is registered as well as after.
        """
        pid = (qs.get("id") or [""])[0]
        raw = (qs.get("path") or [""])[0]
        if pid:
            proj = next((p for p in _CFG["projects"] if p["id"] == pid), None)
            if proj is None:
                return self._json(404, {"error": "project not found"})
            raw = proj["path"]
        root = _valid_root(raw)
        if root is None:
            return self._json(400, {"error": f"not a readable folder: {raw}"})
        candidates = _detect_dev_servers(root)
        html = [c for c in candidates if c["servesHtml"]]
        return self._json(
            200,
            {
                "ok": True,
                "root": str(root),
                "candidates": candidates,
                # Only offered when unambiguous, matching add-time behaviour.
                "suggested": html[0]["url"] if len(html) == 1 else "",
            },
        )

    def _h_projects_preview_url(self) -> None:
        """Set or clear a registered project's dev-server URL.

        Sending an empty `previewUrl` reverts the project to proxied-from-disk,
        which is the right move when the dev server is not running: the static
        proxy still renders something, where a dead URL frames an error page.
        """
        data = self._read_body()
        pid = str(data.get("id", ""))
        url = str(data.get("previewUrl", "") or "").strip().rstrip("/")
        if url and not _valid_target(url):
            return self._json(
                400,
                {
                    "error": "dev server URL must be http://localhost:PORT or http://127.0.0.1:PORT",
                },
            )
        with _QUEUE_LOCK:  # read-modify-write over the shared registry
            proj = next((p for p in _CFG["projects"] if p["id"] == pid), None)
            if proj is None:
                return self._json(404, {"error": "project not found"})
            if url:
                proj["previewUrl"] = url
            else:
                proj.pop("previewUrl", None)
            _save_cfg(_CFG)
        return self._json(200, {"ok": True, "project": proj})

    def _h_projects_select(self) -> None:
        """Connect a registered project: make it the active served root.

        Under `_QUEUE_LOCK` for the same reason as the queue transactions: this
        is a ThreadingHTTPServer, so a concurrent `remove` could drop the project
        from the registry between the lookup and the write below, leaving a
        removed project as `activeId` with `_ROOT` still serving it.
        """
        data = self._read_body()
        pid = str(data.get("id", ""))
        global _ROOT, _TARGET
        with _QUEUE_LOCK:
            proj = next((p for p in _CFG["projects"] if p["id"] == pid), None)
            if proj is None:
                return self._json(404, {"error": "project not found"})
            root = _valid_root(proj["path"])
            if root is None:
                return self._json(400, {"error": f"folder no longer readable: {proj['path']}"})
            _ROOT = str(root)
            _TARGET = ""
            _CFG["activeId"] = pid
            _save_cfg(_CFG)
        return self._json(200, {"ok": True, "project": proj})

    def _h_projects_remove(self) -> None:
        """Remove a project from the registry (does not touch the folder on disk).

        Same lock, same reason: read-modify-write over the shared registry.
        """
        data = self._read_body()
        pid = str(data.get("id", ""))
        global _ROOT
        with _QUEUE_LOCK:
            proj = next((p for p in _CFG["projects"] if p["id"] == pid), None)
            if proj is None:
                return self._json(404, {"error": "project not found"})
            _CFG["projects"] = [p for p in _CFG["projects"] if p["id"] != pid]
            if _CFG.get("activeId") == pid:
                _CFG["activeId"] = ""
                _ROOT = ""
            _save_cfg(_CFG)
        # Release the project's RUNNING resources, not just its registry row.
        # Deliberately outside `_QUEUE_LOCK`: `_stop_dev_proc` escalates
        # SIGTERM→SIGKILL and waits, and `shutdown()` blocks on the accept loop,
        # so holding the registry lock across either would stall every other
        # queue and registry operation for the duration of a process kill.
        # Without this the row disappears while the child dev server, its
        # injecting proxy and the project's static listener keep running with no
        # handle left to reach them, so repeated add/preview/remove accumulates
        # processes, threads and bound ports until something fails to start.
        _stop_dev_proc(pid)
        _stop_static_preview(pid)
        return self._json(200, {"ok": True, "id": pid})

    def _h_pick_folder(self) -> None:
        """Open the native macOS folder chooser (osascript) and return the
        picked absolute path. The backend runs on the user's Mac, so the
        dialog appears locally — the browser never needs the path."""

        if _sys.platform != "darwin":
            return self._json(501, {"error": "native picker is macOS-only"})
        if not _PICK_LOCK.acquire(blocking=False):
            return self._json(409, {"error": "a folder picker is already open"})
        try:
            script = (
                'tell application "System Events" to activate\n'
                'POSIX path of (choose folder with prompt "Select a web app folder for Design Tweak")'
            )
            # Pinned to the system directories, not `PATH` — see `_lsof_fields`.
            osascript = trusted_system_bin("osascript")
            if not osascript:
                return self._json(
                    501,
                    {"error": "native picker is unavailable", "code": "picker_unavailable"},
                )
            r = subprocess.run(
                [osascript, "-e", script],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return self._json(408, {"error": "picker timed out"})
        except OSError as exc:
            return self._json(500, {"error": str(exc)})
        finally:
            _PICK_LOCK.release()
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            if "-128" in err or "canceled" in err.lower():
                return self._json(200, {"ok": False, "canceled": True})
            return self._json(500, {"error": err[-200:] or "picker failed"})
        path = r.stdout.strip().rstrip("/")
        if not path:
            return self._json(200, {"ok": False, "canceled": True})
        return self._json(200, {"ok": True, "path": path})

    def _h_thread(self, qs: dict) -> None:
        """Append a progress note to a COMMENT's thread (or the request's).

        `POST /thread?id=<requestId>&cid=<commentId>` targets one comment — this
        is what the agent uses while working a batch, so each comment's bubble
        tracks its own progress. Omitting `cid` appends a request-level note.

        Body: {"role": "agent"|"user"|"system", "text": "...", "status": "done"?}
        `status` applies to the addressed comment. The request's own status is
        always derived from its comments, never stored.
        """
        rid = (qs.get("id") or [""])[0]
        cid = (qs.get("cid") or [""])[0]
        if not rid or not _ID_RE.match(rid):
            return self._json(400, {"error": "valid id required"})
        if cid and not _ID_RE.match(cid):
            return self._json(400, {"error": "invalid cid"})
        data = self._read_body()
        # `text` is AGENT-authored: it is persisted into the queue JSON and later
        # rendered in the panel, so it goes through the repo's mandatory output
        # redaction before it is stored, not on the way out — a leaked credential
        # must not exist on disk in the first place.
        #
        # URLs first, then credentials — the same order as every other call site
        # (`dashboard/handlers/artifacts.py::_serialize`,
        # `apps/builtins/workflows/server.py::_redact_obj`). The order is
        # load-bearing: `redact_exfiltration_urls` keys off the URL's host, so
        # running the credential pass first would rewrite a token inside a URL to
        # `[REDACTED: credential]` and leave the exfiltration host itself intact.
        # Applied per-field, not to the whole payload: `role` and `status` below
        # are allow-listed to fixed vocabularies, so neither can persist an
        # arbitrary string, and `ts` is generated here.
        text, _ = redact_exfiltration_urls(str(data.get("text", "")).strip())
        text, _ = redact_credentials(text)
        role = str(data.get("role", "agent")).strip() or "agent"
        if role not in ("agent", "user", "system"):
            role = "agent"
        new_status = str(data.get("status", "")).strip()
        if not text and not new_status:
            return self._json(400, {"error": "text or status required"})

        def _txn() -> tuple[int, dict]:
            fp = _find_request(rid)
            if fp is None:
                return 404, {"error": "not found"}
            req = _read_request(fp)
            if req is None:
                return 500, {"error": "request unreadable"}

            entry = {"role": role, "text": text, "ts": _now_iso()}
            if cid:
                target = next((c for c in (req.get("comments") or []) if c.get("cid") == cid), None)
                if target is None:
                    return 404, {"error": f"comment {cid} not in request {rid}"}
                thread = target.get("thread")
                if not isinstance(thread, list):
                    thread = []
                if text:
                    if len(thread) >= MAX_THREAD_ENTRIES:
                        return 429, {
                            "error": (
                                f"comment {cid} already holds {MAX_THREAD_ENTRIES} thread "
                                "entries — the conversation is full"
                            ),
                            "code": "thread_entry_limit",
                        }
                    thread.append(entry)
                target["thread"] = thread
                if new_status in _COMMENT_STATUSES:
                    target["status"] = new_status
            else:
                thread = req.get("thread")
                if not isinstance(thread, list):
                    thread = []
                if text:
                    if len(thread) >= MAX_THREAD_ENTRIES:
                        return 429, {
                            "error": (
                                f"request {rid} already holds {MAX_THREAD_ENTRIES} thread "
                                "entries — the conversation is full"
                            ),
                            "code": "thread_entry_limit",
                        }
                    thread.append(entry)
                req["thread"] = thread
                # A request-level `done` fans out to every comment, so an agent that
                # reports once for the whole batch still resolves the sub-items.
                if new_status == "done":
                    for c in req.get("comments") or []:
                        c["status"] = "done"

            # Any agent activity means this batch is in flight — normalise `state` so
            # it can never contradict the comments. Two drifts to close: an agent
            # writing its own `state` value (which used to make a worked request read
            # back as an unsent draft), and an agent reporting progress on a request
            # that was never formally sealed. A bare progress note is enough evidence:
            # the agent only ever sees a request that was handed to it.
            agent_activity = role in ("agent", "system")
            worked = any(c.get("status") != "new" for c in (req.get("comments") or []))
            if req.get("state") not in ("draft", "sent") or (
                req.get("state") == "draft" and (worked or agent_activity)
            ):
                req["state"] = "sent"
                if not req.get("sentAt"):
                    req["sentAt"] = _now_iso()

            try:
                _write_request(fp, req)
            except OSError as exc:
                return 500, {"error": str(exc)}
            return 200, {
                "ok": True,
                "id": rid,
                "cid": cid,
                "status": _request_status(req),
                "request": _summarize(req),
            }

        with _QUEUE_LOCK:
            try:
                code, body = _txn()
            except _RecordTooLarge as exc:
                # The append was refused and the prior record is intact, so this
                # is a full draft rather than lost work — say which.
                code, body = 413, {"error": str(exc), "code": "record_too_large"}
        return self._json(code, body)

    # ---- source + proxy handlers ----
    def _h_set_source(self) -> None:
        """Set the previewed app source: a folder path (served directly) or a
        localhost dev-server URL (reverse-proxied). Empty value clears."""
        data = self._read_body()
        val = str(data.get("value", data.get("url", ""))).strip()
        global _ROOT, _TARGET
        if not val:
            _ROOT = ""
            _TARGET = ""
            return self._json(200, {"ok": True, "mode": "cleared", "proxyUrl": PROXY_PUBLIC_BASE})
        if val.lower().startswith(("http://", "https://")):
            if not _valid_target(val):
                return self._json(
                    400,
                    {
                        "error": "URL must be http://localhost:PORT or http://127.0.0.1:PORT",
                    },
                )
            _TARGET = val.rstrip("/")
            _ROOT = ""
            return self._json(
                200,
                {
                    "ok": True,
                    "mode": "url",
                    "target": _TARGET,
                    "proxyUrl": PROXY_PUBLIC_BASE,
                },
            )
        root = _valid_root(val)
        if root is None:
            return self._json(400, {"error": f"not a readable folder: {val}"})
        _ROOT = str(root)
        _TARGET = ""
        return self._json(
            200,
            {
                "ok": True,
                "mode": "folder",
                "root": _ROOT,
                "proxyUrl": PROXY_PUBLIC_BASE,
            },
        )

    def _h_inject(self) -> None:
        try:
            js = INJECT_FILE.read_bytes()
        except OSError:
            return self._send_raw(404, "application/javascript", b"// overlay not found")
        return self._send_raw(200, "application/javascript; charset=utf-8", js)

    def _send_raw(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        # Every caller passes either a string literal, a `_guess_ctype` constant
        # (closed extension map — no value is derived from the request path), or
        # `_safe_upstream_ctype()` output. `_header_value` stays as defence in
        # depth at the sink: a single CR/LF reaching send_header would let a
        # caller append headers or a second response body (response splitting).
        self.send_header("Content-Type", _header_value(ctype))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- helpers ----
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    # Created here, not at import time (see the QUEUE_DIR/HANDLED_DIR comment
    # above): this is the real process entry point, reached only when the
    # gateway actually starts this backend -- never by an import alone.
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
