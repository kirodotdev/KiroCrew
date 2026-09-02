"""Durable request and project state for the Design Tweak backend.

``server`` is the composition root and security-policy owner.  Functions that
depend on its mutable state or compatibility seams receive that module as
``runtime`` and resolve collaborators through it at call time, making the
server namespace the authoritative runtime boundary.

Queue mutations are whole-record read-modify-write transactions.  Their caller
must hold ``runtime._QUEUE_LOCK`` from the first read through the final write or
rename.  Socket I/O, subprocess work, and response writes must remain outside
that critical section.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

COMMENT_STATUSES = ("new", "sent", "done")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class PathEscape(ValueError):
    """A candidate path escaped the directory that must contain it."""


class RecordTooLarge(ValueError):
    """A queue record would serialize past the reader's byte ceiling."""


def load_config(runtime: Any) -> JsonObject:
    """Load the registry from ``runtime.CONFIG_FILE``, tolerating bad state."""

    try:
        config = runtime.json.loads(runtime.CONFIG_FILE.read_text("utf-8"))
        if isinstance(config, dict):
            config.setdefault("projects", [])
            config.setdefault("activeId", "")
            config.setdefault("counter", 0)
            return config
    except (OSError, ValueError):
        pass
    return {"projects": [], "activeId": "", "counter": 0}


def save_config(runtime: Any, config: JsonObject) -> None:
    """Atomically persist the project registry without the queue-record cap."""

    runtime._atomic_write_json(runtime.CONFIG_FILE, config)


def active_project(runtime: Any) -> JsonObject | None:
    """Return the configured active project, if it still exists."""

    for project in runtime._CFG["projects"]:
        if project["id"] == runtime._CFG["activeId"]:
            return project
    return None


def next_request_number(runtime: Any, project_id: str = "") -> int:
    """Return the next project-local request number.

    Scoped numbering is derived from queue and history files so there is only
    one source of truth.  Unscoped legacy callers retain the global counter.
    The caller must hold ``runtime._QUEUE_LOCK`` while using this value to create
    and publish a request.
    """

    if not project_id:
        runtime._CFG["counter"] = int(runtime._CFG.get("counter", 0)) + 1
        runtime._save_cfg(runtime._CFG)
        return runtime._CFG["counter"]

    highest = 0
    for directory in (runtime.QUEUE_DIR, runtime.HANDLED_DIR):
        for path in directory.glob("*.json"):
            request = runtime._read_request(path)
            if not request or request.get("projectId") != project_id:
                continue
            try:
                highest = max(highest, int(request.get("number") or 0))
            except (TypeError, ValueError):
                continue
    return highest + 1


def new_request_id() -> str:
    """Mint a filesystem-safe, time-sortable request or comment id."""

    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def utc_now_iso() -> str:
    """Return the persisted timestamp format used by request records."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def pending_files(runtime: Any) -> list[Path]:
    """Return pending request paths in stable filename order."""

    return sorted(runtime.QUEUE_DIR.glob("*.json"))


def contain_path(base: Path | str, candidate: str = "") -> Path:
    """Resolve ``candidate`` and return it only when it remains under ``base``.

    Both sides are realpath-normalized so traversal and symlink escapes collapse
    before the comparison.  The separator check rejects sibling-prefix paths.
    Callers must use the returned path rather than reopening the input path.
    """

    base_real = os.path.realpath(base)
    candidate_real = os.path.realpath(os.path.join(base_real, candidate))
    if not candidate_real.startswith(base_real):
        raise PathEscape(f"{candidate_real!r} is outside {base_real!r}")
    if candidate_real != base_real and not candidate_real[len(base_real) :].startswith(os.sep):
        raise PathEscape(f"{candidate_real!r} is a sibling of {base_real!r}, not inside it")
    return Path(candidate_real)


def request_file(runtime: Any, base: Path, request_id: str) -> Path:
    """Return the contained JSON path for a syntactically safe request id."""

    if not request_id or not runtime._ID_RE.match(request_id):
        raise runtime._PathEscape(f"invalid request id: {request_id!r}")
    return runtime._contained(base, f"{request_id}.json")


def is_kirocrew_internal(runtime: Any, target: Path) -> bool:
    """Return whether ``target`` resolves within a private Kiro Crew tree."""

    real = runtime.os.path.realpath(target)
    for base in runtime._KIROCREW_INTERNAL_DIRS:
        if real == base or real.startswith(base + runtime.os.sep):
            return True
    return False


def valid_target(runtime: Any, url: str) -> bool:
    """Accept only an HTTP URL targeting a dialable loopback port."""

    try:
        parsed = runtime.urlparse(url)
        if parsed.scheme != "http":
            return False
        if (parsed.hostname or "").lower() not in runtime._LOOPBACK_HOSTS:
            return False
        port = parsed.port
    except ValueError:
        return False
    return port is None or 1 <= port <= 65535


def valid_root(runtime: Any, path: str) -> Path | None:
    """Resolve an existing project root after server-owned policy checks."""

    try:
        real = runtime.os.path.realpath(runtime.os.path.expanduser(path))
    except (ValueError, OSError):
        return None
    if set(runtime.Path(real).parts) & runtime._DENIED_ROOT_PARTS:
        return None
    if runtime.is_sensitive_path(real):
        return None
    if runtime.path_contains_sensitive(real):
        return None
    resolved = runtime.Path(real)
    if not resolved.is_dir():
        return None
    return resolved


def request_status(request: JsonObject) -> str:
    """Derive request status from authoritative per-comment statuses."""

    comments = request.get("comments") or []
    if not comments:
        return "draft"
    if all(comment.get("status") == "done" for comment in comments):
        return "done"
    if request.get("state") != "draft" or any(
        comment.get("status") != "new" for comment in comments
    ):
        return "sent"
    return "draft"


def is_draft(runtime: Any, request: JsonObject) -> bool:
    """Return whether a request remains open for new comments."""

    return runtime._request_status(request) == "draft"


def element_name(element: JsonObject) -> str:
    """Build a tolerant human-readable label for a captured element."""

    name = str(element.get("tag") or "")
    element_id = element.get("id")
    classes = element.get("classes")
    if element_id:
        name += f"#{element_id}"
    elif isinstance(classes, (list, tuple)):
        names = [value for value in classes[:2] if isinstance(value, str)]
        if names:
            name += "." + ".".join(names)
    return name


def redact_thread(runtime: Any, thread: object) -> list[JsonObject]:
    """Return a read-tolerant thread, redacted as a floor under the ingest pass.

    ``/thread`` already redacts before writing, so nothing new should reach disk
    unredacted — but ingest is not the only writer. The delivery model hands the
    agent the queue JSON directly (that is how a request reaches it), so an entry
    written into the file rather than posted to ``/thread``, or one persisted
    before the ingest pass existed, would otherwise render verbatim in the panel.
    Redaction is an always-on floor in this repo, so it belongs on both edges.
    """

    if not isinstance(thread, list):
        return []
    result: list[JsonObject] = []
    for entry in thread:
        if not isinstance(entry, dict):
            # Tolerate a malformed file rather than 500 the read path: a 500 here
            # is unrecoverable without deleting the queue file by hand.
            continue
        result.append({**entry, "text": runtime._redact_text(entry.get("text", ""))})
    return result


def summarize_comment(runtime: Any, comment: JsonObject) -> JsonObject:
    """Return the panel-facing shape of one comment."""

    selection = comment.get("selection") or {}
    # Keep only dict elements. ``/submit`` rejects a malformed selection at the
    # boundary, but a queue file written before that guard existed would still
    # crash ``element_name`` here, and this read path must stay recoverable.
    raw_elements = selection.get("elements") or []
    elements = [element for element in raw_elements if isinstance(element, dict)]
    element = elements[0] if elements else {}
    return {
        "cid": comment.get("cid", ""),
        "index": comment.get("index", 0),
        "status": comment.get("status", "new"),
        # Redacted on the way OUT for the same reason as ``thread`` below: the
        # delivery model hands the agent the queue JSON directly, so a comment
        # rewritten in the FILE rather than posted through ``/submit`` (which
        # redacts on ingest) would otherwise render verbatim in the panel.
        "comment": runtime._redact_text(comment.get("comment", "")),
        "createdAt": comment.get("createdAt", ""),
        "element": runtime._el_name(element),
        "locator": element.get("locator", ""),
        # Fallback anchors, so a pin survives its element being deleted (parent) or
        # not existing yet (point). The overlay tries them in that order.
        "parentLocator": element.get("parentLocator", ""),
        "point": element.get("point") or {},
        "count": len(elements),
        "mode": selection.get("mode", "single"),
        "previewUrl": comment.get("previewUrl", ""),
        "projectId": comment.get("projectId", ""),
        "sourceFile": comment.get("sourceFile", ""),
        # Where the agent should edit, and how much to trust it:
        # data-kiro-source → "high", React Fiber → "medium", neither → "low".
        # Independent of how the page was served, so a dev-server project gets
        # BETTER targeting than a proxied static folder, not worse.
        "source": element.get("source") or {},
        "followUpTo": comment.get("followUpTo", ""),
        "thread": runtime._redact_thread(comment.get("thread")),
    }


def summarize_request(runtime: Any, request: JsonObject) -> JsonObject:
    """Return request metadata and its panel-facing comment summaries."""

    comments = request.get("comments") or []
    return {
        "id": request.get("id", ""),
        "number": request.get("number", 0),
        "state": request.get("state", "draft"),
        "status": runtime._request_status(request),
        "createdAt": request.get("createdAt", ""),
        "sentAt": request.get("sentAt", ""),
        # Set only once the panel confirms the prompt actually reached the agent.
        # Sealed-but-not-acknowledged is the stranded state the retry bar exists for.
        "deliveredAt": request.get("deliveredAt", ""),
        "projectId": request.get("projectId", ""),
        "projectRoot": request.get("projectRoot", ""),
        "thread": runtime._redact_thread(request.get("thread")),
        "doneCount": sum(1 for comment in comments if comment.get("status") == "done"),
        "comments": [runtime._summarize_comment(comment) for comment in comments],
    }


def project_for_preview(runtime: Any, preview_url: str) -> tuple[JsonObject | None, str]:
    """Resolve a project and relative file from an owned static preview URL."""

    url = str(preview_url)
    relative = ""
    matched = False
    for project_id, record in runtime._STATIC_SRV.items():
        base = record.get("url") or ""
        if base and url.startswith(f"{base}{project_id}/"):
            relative = url[len(base) :]
            matched = True
            break
    if not matched:
        marker = "/api/proxy/"
        marker_at = url.find(marker)
        if marker_at == -1:
            return None, ""
        relative = url[marker_at + len(marker) :]

    relative = relative.split("?")[0].split("#")[0]
    project_id = relative.split("/", 1)[0] if relative else ""
    rest = relative.split("/", 1)[1] if "/" in relative else ""
    project = next(
        (item for item in runtime._CFG["projects"] if item["id"] == project_id),
        None,
    )
    return project, rest


def project_by_id(runtime: Any, project_id: str) -> JsonObject | None:
    """Look up a registered project by id."""

    if not project_id:
        return None
    return next(
        (project for project in runtime._CFG["projects"] if project["id"] == project_id),
        None,
    )


def resolve_project(runtime: Any, payload: JsonObject) -> tuple[str, str, str]:
    """Resolve ``(project id, project root, contained source file)`` for a capture."""

    preview_url = str(payload.get("previewUrl", ""))
    explicit_id = str(payload.get("projectId", "") or "")
    project = runtime._project_by_id(explicit_id)
    served_relative = ""

    if project is not None:
        url_project, served_relative = runtime._proj_for_preview(preview_url)
        if url_project is not None and url_project["id"] != project["id"]:
            served_relative = ""
    else:
        project, served_relative = runtime._proj_for_preview(preview_url)

    if project is not None:
        root = project["path"]
        return project["id"], root, runtime._contained_source(root, served_relative)
    if runtime._ROOT:
        return (
            explicit_id,
            runtime._ROOT,
            runtime._contained_source(runtime._ROOT, served_relative),
        )
    return explicit_id, "", ""


def contained_source(runtime: Any, root: str, relative: str) -> str:
    """Return a contained source path, or an empty string on escape/route input."""

    if not relative:
        return ""
    try:
        return str(runtime._contained(root, relative))
    except runtime._PathEscape:
        return ""


def sanitize_selection_sources(runtime: Any, selection: Any, root: str) -> None:
    """Contain preview-controlled element source paths in place, failing closed."""

    if not isinstance(selection, dict):
        return
    for element in selection.get("elements") or []:
        if not isinstance(element, dict):
            continue
        source = element.get("source")
        if not isinstance(source, dict) or not source.get("file"):
            continue
        safe = runtime._contained_source(root, str(source["file"])) if root else ""
        if safe:
            source["file"] = safe
        else:
            source["file"] = ""
            source["confidence"] = "low"


def read_request(runtime: Any, path: Path) -> JsonObject | None:
    """Read one contained, size-bounded queue record.

    The size check must precede ``read_text``.  Queue files are writable outside
    this process, and a single oversized file must not poison queue listing.
    """

    try:
        contained = runtime._contained(runtime.DATA_DIR, runtime.os.fspath(path))
        if contained.stat().st_size > runtime.MAX_RECORD_BYTES:
            return None
        request = runtime.json.loads(contained.read_text("utf-8"))
        return request if isinstance(request, dict) else None
    except (runtime._PathEscape, OSError, ValueError):
        return None


def atomic_write_json(
    runtime: Any,
    path: Path,
    payload: JsonObject,
    *,
    max_bytes: int | None = None,
) -> None:
    """Publish complete durable JSON with a same-directory atomic replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = runtime.json.dumps(payload, indent=2).encode("utf-8")
    if max_bytes is not None and len(data) > max_bytes:
        raise runtime._RecordTooLarge(
            f"record would be {len(data)} bytes, over the {max_bytes}-byte limit"
        )

    descriptor, temporary = runtime.tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            written = 0
            while written < len(data):
                count = runtime.os.write(descriptor, data[written:])
                if count <= 0:  # pragma: no cover - os.write normally raises
                    raise OSError(f"short write to {temporary}: {written}/{len(data)} bytes")
                written += count
            runtime.os.fsync(descriptor)
        finally:
            runtime.os.close(descriptor)
        runtime.os.replace(temporary, path)
    finally:
        if runtime.os.path.exists(temporary):
            runtime.os.unlink(temporary)


def write_request(runtime: Any, path: Path, request: JsonObject) -> None:
    """Atomically write a contained request using the shared read/write ceiling."""

    contained = runtime._contained(runtime.DATA_DIR, runtime.os.fspath(path))
    runtime._atomic_write_json(
        contained,
        request,
        max_bytes=runtime.MAX_RECORD_BYTES,
    )


def find_request(runtime: Any, request_id: str) -> Path | None:
    """Locate a request in queue first, then handled history."""

    for directory in (runtime.QUEUE_DIR, runtime.HANDLED_DIR):
        try:
            path = runtime._request_file(directory, request_id)
        except runtime._PathEscape:
            return None
        if path.is_file():
            return path
    return None


def open_draft_file(runtime: Any, project_id: str) -> Path | None:
    """Return the project's single open draft, based on derived status."""

    for path in runtime._pending_files():
        request = runtime._read_request(path)
        if request and request.get("projectId") == project_id and runtime._is_draft(request):
            return path
    return None


__all__ = [
    "COMMENT_STATUSES",
    "JsonObject",
    "PathEscape",
    "RecordTooLarge",
    "REQUEST_ID_RE",
    "active_project",
    "atomic_write_json",
    "contain_path",
    "contained_source",
    "element_name",
    "find_request",
    "is_draft",
    "is_kirocrew_internal",
    "load_config",
    "new_request_id",
    "next_request_number",
    "open_draft_file",
    "pending_files",
    "project_by_id",
    "project_for_preview",
    "read_request",
    "redact_thread",
    "request_file",
    "request_status",
    "resolve_project",
    "sanitize_selection_sources",
    "save_config",
    "summarize_comment",
    "summarize_request",
    "utc_now_iso",
    "valid_root",
    "valid_target",
    "write_request",
]
