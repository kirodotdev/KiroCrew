"""Session documents: the documents chat sessions produced, listed and materialized.

``GET /api/artifacts/session-docs`` lists the non-code documents across chat sessions,
and ``POST /api/artifacts/materialize`` turns one into a pinned artifact, reporting a
slug collision with an existing record.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from aiohttp import web

from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    ArtifactError,
    ArtifactValidationError,
    get_default_store,
    is_document_path,
)
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import _serialize
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _artifact_source_for_request,
    _audit,
    _clean_origin_session_key,
    _err,
    _json_response,
    _non_owner_refusal,
    _read_json_body,
    _run_off_loop,
    logger,
)
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_with_identity, stat_identity
from kiro_crew.security import is_sensitive_path


def _collect_session_docs(
    conversation_log: Any,
    saved_map: dict[str, str],
    session_key: str | None = None,
) -> list[dict[str, Any]]:
    """Scan ALL sessions for non-code document file-changes (blocking).

    Returns one entry per distinct document path (latest session wins), each
    ``{path, name, updated_at, session_key, session_title, message_ts, saved,
    slug}`` — with the **TRUE on-disk path** (never redacted). ``saved`` is True
    when the path already backs a real (materialized) artifact. Sorted
    newest-first. Runs OFF the event loop — reads every session's jsonl.

    INTERNAL ONLY — the raw ``path``/``name``/``session_title`` may contain a
    credential-shaped substring. Callers that reach an external surface MUST go
    through :func:`_scan_session_docs`, which redacts the display fields. The
    authorization scan (:func:`_recorded_doc_identities`) uses this raw result
    directly because it must ``stat`` the true path — redaction there would
    corrupt a path that merely contains a credential-shaped substring (e.g. a
    temp dir ``/var/folders/.../<hash>/``), silently emptying the materialize
    allowlist so a legitimate document could not be saved.
    """
    best: dict[str, dict[str, Any]] = {}
    try:
        sessions = conversation_log.list_sessions()
    except Exception as exc:  # noqa: BLE001 — a corrupt history dir must not 500 the page
        logger.warning("session-docs: list_sessions failed: %s", exc)
        return []
    for sess in sessions:
        if not isinstance(sess, dict):
            continue  # malformed session entry — skip, never crash the scan
        key = sess.get("key")
        if not key:
            continue
        # When scoping to one session, dashboard slots map to the history key
        # ``dashboard_{slot}`` (see state.py) — accept either form.
        if session_key and key not in (session_key, f"dashboard_{session_key}"):
            continue
        # Untrusted history: coerce the timestamp defensively — a non-numeric or
        # non-finite ``modified`` must not crash the whole scan (it only orders
        # "latest session wins"), so fall back to 0.0.
        try:
            modified = float(sess.get("modified") or 0.0)
        except (TypeError, ValueError):
            modified = 0.0
        if modified != modified or modified in (float("inf"), float("-inf")):  # NaN / inf
            modified = 0.0
        session_title = sess.get("title") or key
        try:
            file_change_reader = getattr(conversation_log, "read_file_change_messages", None)
            msgs = (
                file_change_reader(key)
                if callable(file_change_reader)
                else conversation_log.read_messages(key)
            )
        except Exception:  # noqa: BLE001 — skip an unreadable session, keep scanning
            continue
        for m in msgs:
            if not isinstance(m, dict):
                continue  # malformed message — skip
            meta = m.get("meta")
            if not isinstance(meta, dict):
                continue  # meta absent or wrong shape — nothing to scan
            file_changes = meta.get("file_changes")
            if not isinstance(file_changes, list):
                continue  # file_changes absent or wrong shape
            for fc in file_changes:
                if not isinstance(fc, dict):
                    continue  # malformed file-change entry — skip
                raw_p = fc.get("path")
                p = (raw_p if isinstance(raw_p, str) else "").strip()
                if not p or not is_document_path(p):
                    continue
                prev = best.get(p)
                if prev is None or modified >= prev["_mtime"]:
                    best[p] = {
                        "path": p,
                        "name": os.path.basename(p) or p,
                        "session_key": key,
                        "session_title": session_title,
                        "message_ts": m.get("ts") or "",
                        "_mtime": modified,
                    }

    out: list[dict[str, Any]] = []
    for e in sorted(best.values(), key=lambda d: d["_mtime"], reverse=True):
        mt = e.pop("_mtime")
        raw_path = e["path"]
        e["updated_at"] = datetime.fromtimestamp(mt).isoformat() if mt else ""
        # saved/slug are keyed by the real source_path.
        e["saved"] = raw_path in saved_map
        e["slug"] = saved_map.get(raw_path, "")
        out.append(e)
    return out


def _scan_session_docs(
    conversation_log: Any,
    saved_map: dict[str, str],
    session_key: str | None = None,
) -> list[dict[str, Any]]:
    """Display-safe session-doc scan for the ``/session-docs`` API.

    Wraps :func:`_collect_session_docs` and redacts every display field
    (``path``, ``name``, ``session_title``) through the credential/exfiltration
    redactors before the result leaves the process. Redaction is identity for
    normal content, so ordinary paths still round-trip through ``/materialize``;
    a path that actually contains a secret becomes intentionally unmatchable
    (safe to refuse). Redaction lives HERE — at the one boundary that reaches an
    external surface — so no shared flag can accidentally expose a raw path.
    """

    def _redact(text: str) -> str:
        from kiro_crew.security import redact

        cleaned = redact(text or "")
        return cleaned

    out = _collect_session_docs(conversation_log, saved_map, session_key)
    for e in out:
        e["path"] = _redact(e["path"])
        e["name"] = _redact(e["name"])
        e["session_title"] = _redact(e["session_title"])
    return out


def _recorded_doc_identities(conversation_log: Any) -> set[tuple[int, int]]:
    """``(st_dev, st_ino)`` identities of documents recorded in ``file_changes``.

    Authorization allowlist for ``/materialize``: only documents the agent
    produced in a chat may be materialized. Matching the ``fstat`` of the
    *opened* descriptor against these identities (rather than re-resolving the
    request path a second time) proves the file actually read is the very inode
    an allowlisted document resolves to right now. A symlink- or directory-swap
    slipped in between ``realpath`` and ``open`` cannot smuggle in a different
    (unauthorized) file, because that file's ``(dev, ino)`` is not in this set —
    the authorized target and the read target are guaranteed identical.

    Identities are resolved through ``hooks.stat_identity`` — the centralized
    sensitive-path gate — so a recorded path that resolves into ``~/.aws`` etc.
    is refused rather than ``stat``'d directly. Only absolute recorded paths are
    trustworthy; a relative path would resolve against the gateway CWD (not the
    session's project) and could match an unrelated same-named file, so relative
    entries are skipped.
    """
    out: set[tuple[int, int]] = set()
    # Use the RAW collector (not _scan_session_docs): this is an in-process
    # authorization scan whose result never reaches an external surface, and it
    # must ``stat`` the TRUE path — a credential-shaped substring in the path
    # (e.g. a temp-dir hash) would otherwise be redacted to a non-existent path
    # and silently empty the allowlist, refusing a legitimate materialize.
    for e in _collect_session_docs(conversation_log, {}):
        expanded = os.path.expanduser(e["path"])
        if not os.path.isabs(expanded):
            continue
        ident = stat_identity(expanded)
        if ident is not None:
            out.add(ident)
    return out


def _collision_report(art: Any) -> str:
    """Name the slug ``art``'s name derives to when the store had to suffix it.

    Reads ``Artifact.slug_collided_with``, which ``ArtifactStore.create`` sets
    from its uniquifier — the one place that knows a suffix happened. Nothing is
    inferred here, so none of the caveats an inference needs apply: a record
    reused rather than created, or renamed by ``update()`` without recomputing
    its slug, carries an empty field and cannot report a phantom collision.
    """
    return str(getattr(art, "slug_collided_with", "") or "")


def _materialize_and_pin(
    path: str, conversation_log: Any, source: str = "chat", session_key: str = ""
) -> tuple[Any, str]:
    """Create (or reuse) a file-backed artifact from ``path`` and mark it saved.

    Returns ``(artifact, slug_collided_with)``, where the second element names
    the slug the artifact's name derives to when the store had to append a
    numeric suffix because that slug was taken, and is empty otherwise. It is
    computed here, not by the caller, because only this function knows whether a
    create happened: the reuse branch below collides with nothing, and
    :meth:`ArtifactStore.update` can rename an artifact without recomputing its
    slug, so a reused record's name may derive to a slug that differs from its
    own with no collision ever having occurred.

    Idempotent: if an artifact already backs this path, just pin it. Otherwise
    the file is AUTHORIZED and READ through a single ``O_NOFOLLOW`` descriptor in
    the centralized ``hooks`` chokepoint
    (:func:`hooks.safe_read_file_bytes_with_identity`): the opened inode's
    ``fstat`` identity ``(st_dev, st_ino)`` MUST match a document recorded in the
    chat history's ``file_changes`` (:func:`_recorded_doc_identities`).
    Authorizing the opened descriptor — rather than re-resolving the path a
    second time for the read — closes the symlink/dir-swap TOCTOU window: the
    file we authorize is exactly the inode we read. Sensitive resolved
    targets (``~/.aws`` …) and non-documents are refused up front. Blocking;
    call via ``_run_off_loop``.
    """
    store = get_default_store()
    expanded = os.path.expanduser(path)
    # Reject relative paths — they'd resolve against the gateway CWD rather than
    # the session's project dir, so a same-named unrelated file could satisfy the
    # allowlist. Recorded document paths are absolute; require it.
    if not os.path.isabs(expanded):
        raise ArtifactValidationError("document path must be absolute")
    canonical = os.path.realpath(expanded)
    existing = store.find_by_source_path(path) or store.find_by_source_path(canonical)
    if existing is not None:
        return store.set_pinned(existing.slug, True), ""
    if not is_document_path(canonical):
        raise ArtifactValidationError("only document files can be saved this way")
    # Defense in depth: a resolved target under a sensitive dir is never a chat
    # document — refuse before opening.
    if is_sensitive_path(canonical):
        raise ArtifactValidationError("path is not a document from your chat history")
    # Authorize AND read through the centralized hooks chokepoint: the helper
    # opens the file ONCE with O_NOFOLLOW and only returns bytes if the opened
    # inode's identity is in the recorded-documents allowlist, so a symlink/dir
    # swap between realpath() and open() cannot substitute an unauthorized file.
    try:
        data = safe_read_file_bytes_with_identity(
            canonical, _recorded_doc_identities(conversation_log)
        )
    except PermissionError as exc:
        raise ArtifactValidationError("path is not a document from your chat history") from exc
    except FileTooLargeError as exc:
        raise ArtifactValidationError(str(exc)) from exc
    if data is None:
        raise ArtifactError("cannot read file")
    # Enforce the artifact content cap up front (store also rejects > 1 MiB) so
    # a large document doesn't waste memory/executor time before create().
    if len(data) > MAX_CONTENT_BYTES:
        raise ArtifactValidationError(f"document exceeds {MAX_CONTENT_BYTES} bytes")
    content = data.decode("utf-8", errors="replace")
    art = store.create(
        name=os.path.basename(canonical) or canonical,
        content=content,
        source=source,
        source_path=canonical,
        session_key=session_key,
    )
    # No slug parameter reaches this path, so the store always derives the slug
    # from the name and silently appends a numeric suffix when that slug is taken;
    # the shared report therefore needs no explicit-slug argument here. The reuse
    # branch above returns before this point, so this only ever describes a create.
    return store.set_pinned(art.slug, True), _collision_report(art)


async def api_artifact_session_docs(request: web.Request) -> web.Response:
    """GET /api/artifacts/session-docs — virtual list of non-code documents
    produced across all chat sessions (the "All" firehose). Read-only; creates
    no artifact records."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_session_docs",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot list session docs", status=403)
    clog = getattr(state, "conversation_log", None)
    if clog is None:
        _audit(
            tool="artifact_session_docs",
            request=request,
            outcome="success",
            extra={"count": 0},
        )
        return _json_response({"docs": []})
    store = get_default_store()
    session = request.query.get("session") or None

    def work() -> list[dict[str, Any]]:
        # Map pinned artifacts by their backing file path → slug, so each
        # session doc can report saved-status and a slug to unsave against.
        saved_map = {
            a.source_path: a.slug
            for a in store.list()
            if getattr(a, "source_path", "") and getattr(a, "pinned", False)
        }
        return _scan_session_docs(clog, saved_map, session)

    try:
        docs = await _run_off_loop(work)
    except Exception as exc:  # noqa: BLE001 — audit + redacted 500 on any scan/list failure
        from kiro_crew.security import redact

        safe_err = redact(str(exc))
        _audit(
            tool="artifact_session_docs",
            request=request,
            outcome="error",
            error=safe_err,
        )
        return _err("failed to list session documents", status=500)
    _audit(
        tool="artifact_session_docs",
        request=request,
        outcome="success",
        extra={"count": len(docs)},
    )
    return _json_response({"docs": docs})


async def api_artifact_materialize(request: web.Request) -> web.Response:
    """POST /api/artifacts/materialize — turn a session document path into a
    real, saved (pinned) file-backed artifact. Body: ``{"path": "..."}``.
    Idempotent by source_path."""
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_materialize")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot save artifacts", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        _audit(tool="artifact_materialize", request=request, outcome="denied", error=str(exc))
        return _err(str(exc))
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="denied",
            error="path required (must be a string)",
        )
        return _err("path required (must be a string)")
    path = path.strip()
    # Redacted copy for audit/error metadata — never emit a raw (LLM-influenced)
    # path into the SEL audit log (credential/exfiltration redaction rule).
    from kiro_crew.security import redact

    audit_path = redact(path)
    clog = getattr(state, "conversation_log", None)
    if clog is None:
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="error",
            error="conversation log unavailable",
            extra={"path": audit_path},
        )
        return _err("conversation log unavailable", status=500)
    try:
        art, collided_with = await _run_off_loop(
            lambda: _materialize_and_pin(
                path,
                clog,
                _artifact_source_for_request(request),
                _clean_origin_session_key(body.get("origin_session_key")),
            )
        )
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"path": audit_path},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"path": audit_path},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_materialize",
        request=request,
        outcome="success",
        extra={"path": audit_path, "slug": art.slug},
    )
    # Report a de-duplicated slug so a client promoting a corrected document
    # learns it landed at a suffixed slug while the canonical one keeps serving
    # the older text. This response only: the value describes this call, so the
    # artifact record would carry it empty on every GET and LIST, and would keep
    # naming a collision after the artifact that caused it is deleted.
    payload = _serialize(art, include_content=True)
    payload["slug_collided_with"] = collided_with
    return _json_response(payload)
