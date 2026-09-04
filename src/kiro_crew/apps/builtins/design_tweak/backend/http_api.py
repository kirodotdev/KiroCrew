"""HTTP API adapter for the Design Tweak backend.

The concrete server module binds :attr:`Handler.runtime` on a subclass.  Every
mutable value and collaborator is resolved through that facade at call time,
making the server namespace authoritative while keeping transport concerns out
of the composition root.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from typing import Any

#: Product half of the HTTP ``Server:`` header. A wire identifier rather than
#: prose, so it keeps the joined spelling; one definition so the unbound default
#: and the bound value cannot drift apart.
_SERVER_TOKEN = "KiroCrew-SelectToEdit/"  # brand-ok: Server-header token


class IncompleteBody(ValueError):
    """The client sent fewer bytes than its Content-Length promised."""


class Handler(BaseHTTPRequestHandler):
    """Design Tweak's authenticated JSON API.

    A bound subclass must set ``runtime`` and implement ``_h_pick_folder``.  The
    native picker remains in ``server.py`` so the audited subprocess sink stays
    composition-root owned.
    """

    runtime: Any = None
    server_version = _SERVER_TOKEN + "unbound"
    timeout = 30

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.runtime is not None:
            cls.server_version = _SERVER_TOKEN + cls.runtime.VERSION
            cls.timeout = cls.runtime._CLIENT_READ_TIMEOUT

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        rt = self.runtime
        url = rt.urlparse(self.path)
        route = url.path.rstrip("/") or "/"
        if route.startswith("/api/"):
            route = route[4:] or "/"
        elif route == "/api":
            route = "/"
        return route, rt.parse_qs(url.query)

    def _read_raw_body(self) -> bytes:
        """Read and cache one correctly framed, size-bounded request body."""

        rt = self.runtime
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        if length > rt.MAX_BODY_BYTES:
            raise ValueError("payload too large")
        body = self.rfile.read(length)
        if len(body) != length:
            # Missing bytes could otherwise become the next keep-alive request.
            self.close_connection = True
            raise rt._IncompleteBody("request body shorter than Content-Length")
        return body

    def _authorized(self, method: str, body: bytes) -> bool:
        """Verify gateway HMAC before dispatch, except for liveness probes."""

        rt = self.runtime
        route = rt.urlparse(self.path).path.rstrip("/")
        if route in ("", "/health", "/api", "/api/health"):
            return True
        if rt.verify_proxy_request(
            self.headers.get("X-KiroCrew-Proxy", ""),
            method=method,
            target=self.path,
            body=body,
        ):
            return True
        # Query strings can contain project paths, so audit only the route path.
        rt.sel().log_api_access(
            caller=rt.APP_NAME,
            operation="proxy_auth_failed",
            outcome="denied",
            source="builtin-app",
            resources=rt.urlparse(self.path).path,
        )
        self._json(
            401,
            {
                "error": "invalid or missing proxy signature",
                "code": "invalid_proxy_signature",
            },
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        rt = self.runtime
        if not self._authorized("GET", b""):
            return
        try:
            route, query = self._route()
            if route in ("/", "/health"):
                return self._json(
                    200,
                    {
                        "status": "ok",
                        "app": rt.APP_NAME,
                        "version": rt.VERSION,
                        "pending": len(rt._pending_files()),
                        "dataDir": str(rt.DATA_DIR),
                    },
                )
            if route == "/queue":
                pending = []
                for path in rt._pending_files():
                    request = rt._read_request(path)
                    if request is not None:
                        pending.append(rt._summarize(request))
                pending.sort(key=lambda request: request.get("number") or 0)
                return self._json(200, {"pending": pending})
            if route == "/latest":
                files = rt._pending_files()
                if not files:
                    return self._json(200, {})
                newest, newest_number = None, -1
                for path in files:
                    request = rt._read_request(path)
                    if request and (request.get("number") or 0) > newest_number:
                        newest = request
                        newest_number = request.get("number") or 0
                # `/latest` returns the full record, so free-text threads need the
                # same output-redaction floor as the summary paths.
                if newest is not None:
                    newest = {
                        **newest,
                        "thread": rt._redact_thread(newest.get("thread")),
                        "comments": [
                            {**comment, "thread": rt._redact_thread(comment.get("thread"))}
                            for comment in (newest.get("comments") or [])
                        ],
                    }
                return self._json(200, newest or {})
            if route == "/projects":
                return self._h_projects_list()
            if route == "/detect-dev-server":
                return self._h_detect_dev_server(query)
            if route == "/history":
                history = []
                for path in sorted(rt.HANDLED_DIR.glob("*.json"), reverse=True)[:50]:
                    request = rt._read_request(path)
                    if request is not None:
                        history.append(rt._summarize(request))
                return self._json(200, {"history": history})
            if route == "/proxy-inject.js":
                return self._h_inject()
            if route == "/proxy" or route.startswith("/proxy/"):
                # Project-controlled content must never run on dashboard origin.
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
        rt = self.runtime
        try:
            body = self._read_raw_body()
        except rt._IncompleteBody as exc:
            return self._json(400, {"error": str(exc), "code": "incomplete_body"})
        except ValueError as exc:
            return self._json(413, {"error": str(exc)})
        except OSError:
            self.close_connection = True
            return self._json(
                408,
                {"error": "request body timed out", "code": "body_timeout"},
            )
        if not self._authorized("POST", body):
            return
        self._cached_body = body
        try:
            route, query = self._route()
            if route == "/submit":
                return self._h_submit()
            if route == "/clear":
                return self._h_clear(query)
            if route == "/delete":
                return self._h_delete(query)
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
                return self._h_dev_server_start(query)
            if route == "/dev-server/stop":
                return self._h_dev_server_stop(query)
            if route == "/pick-folder":
                return self._h_pick_folder()
            if route == "/send":
                return self._h_send(query)
            if route == "/delivered":
                return self._h_delivered(query)
            if route == "/delete-comment":
                return self._h_delete_comment(query)
            if route == "/thread":
                return self._h_thread(query)
            return self._json(404, {"error": f"POST {route} not found"})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": str(exc)})

    def _read_body(self) -> dict[str, Any]:
        rt = self.runtime
        raw = getattr(self, "_cached_body", b"")
        if not raw:
            return {}
        data = rt.json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("payload must be a JSON object")
        return data

    def _h_submit(self) -> None:
        """Append a captured comment to the project's single open draft."""

        rt = self.runtime
        payload = self._read_body()
        if payload.get("type") != "visual_edit_request":
            return self._json(400, {"error": "type must be 'visual_edit_request'"})
        selection = payload.get("selection") or {}
        elements = selection.get("elements") if isinstance(selection, dict) else None
        if not isinstance(elements, list) or not elements:
            return self._json(
                400,
                {
                    "error": "selection.elements is required",
                    "code": "selection_required",
                },
            )
        if not all(isinstance(element, dict) for element in elements):
            return self._json(
                400,
                {
                    "error": "selection.elements must contain only objects",
                    "code": "selection_malformed",
                },
            )
        # Reject shapes that would make every later queue summary unreadable.
        for element in elements:
            if not all(
                isinstance(element[key], str)
                for key in ("tag", "id")
                if element.get(key) is not None
            ):
                return self._json(
                    400,
                    {
                        "error": "selection.elements[].tag and .id must be strings",
                        "code": "selection_malformed",
                    },
                )
            classes = element.get("classes")
            if classes is not None and (
                not isinstance(classes, list)
                or not all(isinstance(value, str) for value in classes)
            ):
                return self._json(
                    400,
                    {
                        "error": "selection.elements[].classes must be a list of strings",
                        "code": "selection_malformed",
                    },
                )

        preview_url = str(payload.get("previewUrl", ""))
        project_id, project_root, source_file = rt._resolve_project(payload)
        rt._sanitize_selection_sources(selection, project_root)

        def transaction() -> tuple[int, dict[str, Any]]:
            # Find/create, number, append, and publish are one transaction.  A
            # split lets two handler threads lose a comment or duplicate a draft.
            path = rt._open_draft_file(project_id)
            if path is None:
                request_id = rt._new_id()
                request: dict[str, Any] = {
                    "type": "visual_edit_batch",
                    "id": request_id,
                    "number": rt._next_number(project_id),
                    "state": "draft",
                    "projectId": project_id,
                    "projectRoot": project_root,
                    "createdAt": rt._now_iso(),
                    "sentAt": "",
                    "thread": [],
                    "comments": [],
                }
                path = rt._request_file(rt.QUEUE_DIR, request_id)
            else:
                existing = rt._read_request(path)
                if existing is None:
                    return 500, {"error": "draft request unreadable"}
                request = existing

            comments: list[dict[str, Any]] = request.setdefault("comments", [])
            if len(comments) >= rt.MAX_DRAFT_COMMENTS:
                return 429, {
                    "error": (
                        f"this request already holds {rt.MAX_DRAFT_COMMENTS} comments — "
                        "send or clear it before adding more"
                    ),
                    "code": "draft_comment_limit",
                }

            # Lookup ids are always server-minted strings, never payload values.
            comment_id = rt._new_id()
            created_at = payload.get("createdAt") or rt._now_iso()
            follow_up_to = str(payload.get("followUpTo", "") or "")
            if follow_up_to and not rt._ID_RE.match(follow_up_to):
                follow_up_to = ""
            comment = {
                "cid": comment_id,
                "index": len(comments) + 1,
                "status": "new",
                "comment": str(payload.get("comment", "")),
                "createdAt": created_at,
                "selection": selection,
                "previewUrl": preview_url,
                "projectId": project_id,
                "sourceFile": source_file,
                "followUpTo": follow_up_to,
                "thread": [
                    {
                        "role": "user",
                        "text": str(payload.get("comment", "")),
                        "ts": created_at,
                    }
                ],
            }
            if rt._TARGET and not project_root:
                comment["devServer"] = rt._TARGET
            comments.append(comment)
            rt._write_request(path, request)
            return 200, {
                "ok": True,
                "id": request["id"],
                "number": request["number"],
                "state": request["state"],
                "cid": comment_id,
                "index": comment["index"],
                "label": f"{request['number']}.{comment['index']}",
                "commentCount": len(comments),
                "savedTo": str(path),
            }

        with rt._QUEUE_LOCK:
            try:
                code, response = transaction()
            except rt._RecordTooLarge as exc:
                code = 413
                response = {"error": str(exc), "code": "record_too_large"}
        return self._json(code, response)

    def _h_send(self, query: dict[str, list[str]]) -> None:
        """Atomically seal a draft and return the authoritative delivery snapshot."""

        rt = self.runtime
        request_id = (query.get("id") or [""])[0]
        if not request_id or not rt._ID_RE.match(request_id):
            return self._json(400, {"error": "valid id required"})
        path = rt._request_file(rt.QUEUE_DIR, request_id)

        def transaction() -> tuple[int, dict[str, Any]]:
            if not path.is_file():
                return 404, {"error": "not found"}
            request = rt._read_request(path)
            if request is None:
                return 500, {"error": "request unreadable"}
            comments = request.get("comments") or []
            if not comments:
                return 400, {"error": "request has no comments"}
            if not rt._is_draft(request):
                return 200, {
                    "ok": True,
                    "already": True,
                    "request": rt._summarize(request),
                }
            request["state"] = "sent"
            request["sentAt"] = rt._now_iso()
            for comment in comments:
                if comment.get("status") == "new":
                    comment["status"] = "sent"
            rt._write_request(path, request)
            return 200, {"ok": True, "request": rt._summarize(request)}

        with rt._QUEUE_LOCK:
            try:
                code, response = transaction()
            except rt._RecordTooLarge as exc:
                code = 413
                response = {"error": str(exc), "code": "record_too_large"}
        return self._json(code, response)

    def _h_delivered(self, query: dict[str, list[str]]) -> None:
        """Idempotently acknowledge that a sealed prompt reached the agent."""

        rt = self.runtime
        request_id = (query.get("id") or [""])[0]
        if not request_id or not rt._ID_RE.match(request_id):
            return self._json(
                400,
                {"error": "valid id required", "code": "id_required"},
            )
        path = rt._request_file(rt.QUEUE_DIR, request_id)

        def transaction() -> tuple[int, dict[str, Any]]:
            if not path.is_file():
                return 404, {"error": "not found", "code": "not_found"}
            request = rt._read_request(path)
            if request is None:
                return 500, {"error": "request unreadable", "code": "unreadable"}
            if rt._is_draft(request):
                return 409, {
                    "error": "request is still a draft — nothing was dispatched",
                    "code": "not_sealed",
                }
            if not request.get("deliveredAt"):
                request["deliveredAt"] = rt._now_iso()
                rt._write_request(path, request)
            return 200, {"ok": True, "request": rt._summarize(request)}

        with rt._QUEUE_LOCK:
            try:
                code, response = transaction()
            except rt._RecordTooLarge as exc:
                code = 413
                response = {"error": str(exc), "code": "record_too_large"}
        return self._json(code, response)

    def _h_delete_comment(self, query: dict[str, list[str]]) -> None:
        """Delete one draft comment; sent batches are immutable."""

        rt = self.runtime
        request_id = (query.get("id") or [""])[0]
        comment_id = (query.get("cid") or [""])[0]
        if (
            not request_id
            or not rt._ID_RE.match(request_id)
            or not comment_id
            or not rt._ID_RE.match(comment_id)
        ):
            return self._json(400, {"error": "valid id and cid required"})
        path = rt._request_file(rt.QUEUE_DIR, request_id)

        def transaction() -> tuple[int, dict[str, Any]]:
            if not path.is_file():
                return 404, {"error": "not found"}
            request = rt._read_request(path)
            if request is None:
                return 500, {"error": "request unreadable"}
            if not rt._is_draft(request):
                return 409, {"error": "request already sent — cannot remove comments"}
            comments = request.get("comments") or []
            kept = [comment for comment in comments if comment.get("cid") != comment_id]
            if len(kept) == len(comments):
                return 404, {"error": "comment not found"}
            for index, comment in enumerate(kept, start=1):
                comment["index"] = index
            request["comments"] = kept
            if not kept:
                try:
                    path.unlink()
                except OSError as exc:
                    return 500, {"error": str(exc)}
                return 200, {"ok": True, "id": request_id, "removedRequest": True}
            rt._write_request(path, request)
            return 200, {
                "ok": True,
                "id": request_id,
                "cid": comment_id,
                "request": rt._summarize(request),
            }

        with rt._QUEUE_LOCK:
            try:
                code, response = transaction()
            except rt._RecordTooLarge as exc:
                code = 413
                response = {"error": str(exc), "code": "record_too_large"}
        return self._json(code, response)

    def _h_clear(self, query: dict[str, list[str]]) -> None:
        rt = self.runtime
        request_id = (query.get("id") or [""])[0]
        if not request_id or not rt._ID_RE.match(request_id):
            return self._json(400, {"error": "valid id required"})
        source = rt._request_file(rt.QUEUE_DIR, request_id)
        destination = rt._request_file(rt.HANDLED_DIR, request_id)

        def transaction() -> tuple[int, dict[str, Any]]:
            # Serialize the rename with writers so a stale append cannot resurrect
            # an archived request in queue/.
            if not source.exists():
                return 404, {"error": "not found"}
            try:
                source.replace(destination)
            except OSError as exc:
                return 500, {"error": str(exc)}
            return 200, {"ok": True, "id": request_id}

        with rt._QUEUE_LOCK:
            try:
                code, response = transaction()
            except rt._RecordTooLarge as exc:
                code = 413
                response = {"error": str(exc), "code": "record_too_large"}
        return self._json(code, response)

    def _h_delete(self, query: dict[str, list[str]]) -> None:
        rt = self.runtime
        request_id = (query.get("id") or [""])[0]
        if not request_id or not rt._ID_RE.match(request_id):
            return self._json(400, {"error": "valid id required"})

        def transaction() -> tuple[int, dict[str, Any]] | None:
            removed = False
            for directory in (rt.QUEUE_DIR, rt.HANDLED_DIR):
                path = rt._request_file(directory, request_id)
                if path.exists():
                    try:
                        path.unlink()
                        removed = True
                    except OSError as exc:
                        return 500, {"error": str(exc)}
            if not removed:
                return 404, {"error": "not found"}
            return None

        # Serialize unlink with writers so a stale append cannot recreate it.
        with rt._QUEUE_LOCK:
            failed = transaction()
        if failed is not None:
            return self._json(*failed)
        return self._json(200, {"ok": True, "id": request_id, "deleted": True})

    def _h_projects_list(self) -> None:
        rt = self.runtime
        active = rt._active_project()
        serving = bool(rt._ROOT and active and str(rt.Path(active["path"]).resolve()) == rt._ROOT)
        projects = []
        for project in rt._CFG["projects"]:
            row = dict(project)
            root = rt._valid_root(project["path"])
            row.update(
                rt._classify_project(root)
                if root
                else {
                    "needsDevServer": False,
                    "devCommand": "",
                    "unbundledEntry": "",
                    "hasEntry": False,
                }
            )
            row["devRunning"] = rt._dev_proc_alive(project["id"])
            live = rt._DEV_PROCS.get(project["id"]) or {}
            if live.get("proxyUrl"):
                row["previewUrl"] = live["proxyUrl"]
                row["devUrl"] = live.get("url", "")
            elif rt._valid_target(str(row.get("previewUrl") or "").strip()):
                # Persisted upstream URLs must be re-framed through the injecting
                # proxy.  A bind failure may never fall back to the bare URL.
                dev_url = str(row["previewUrl"]).strip()
                row["previewUrl"] = rt._front_with_proxy(project["id"], dev_url)
                row["devUrl"] = dev_url
            elif root is not None and not row.get("previewUrl"):
                static_url = rt._static_preview_url(project["id"])
                if static_url:
                    row["previewUrl"] = static_url
                    row["previewMode"] = "static"
            elif row.get("previewUrl"):
                # Never return a persisted value that no longer proves loopback.
                row["previewUrl"] = ""
            projects.append(row)
        return self._json(
            200,
            {
                "projects": projects,
                "activeId": rt._CFG["activeId"],
                "serving": serving,
                "version": rt.VERSION,
            },
        )

    def _h_dev_server_start(self, query: dict[str, list[str]]) -> None:
        rt = self.runtime
        project_id = (query.get("id") or [""])[0]
        project = next(
            (item for item in rt._CFG["projects"] if item["id"] == project_id),
            None,
        )
        if project is None:
            return self._json(404, {"error": "project not found"})
        root = rt._valid_root(project["path"])
        if root is None:
            return self._json(
                400,
                {"error": f"folder no longer readable: {project['path']}"},
            )

        adopted = rt._auto_dev_server(root)
        if adopted:
            framed = rt._front_with_proxy(project_id, adopted)
            return self._json(
                200,
                {
                    "ok": True,
                    "url": framed,
                    "devUrl": adopted,
                    "adopted": True,
                    "injected": bool(framed) and framed != adopted,
                    "project": project,
                },
            )

        result = rt._start_dev_proc(project_id, root)
        if not result.get("ok"):
            # Starting is a probe-like operation; its error text is the answer.
            return self._json(200, result)
        return self._json(200, {**result, "project": project})

    def _h_dev_server_stop(self, query: dict[str, list[str]]) -> None:
        rt = self.runtime
        project_id = (query.get("id") or [""])[0]
        project = next(
            (item for item in rt._CFG["projects"] if item["id"] == project_id),
            None,
        )
        if project is None:
            return self._json(404, {"error": "project not found"})

        # Process teardown can wait for TERM/KILL escalation, so it must precede
        # and remain outside the registry lock.
        stopped = rt._stop_dev_proc(project_id)
        with rt._QUEUE_LOCK:
            live = next(
                (item for item in rt._CFG["projects"] if item["id"] == project_id),
                None,
            )
            if live is not None:
                live.pop("previewUrl", None)
                rt._save_cfg(rt._CFG)
                project = live
        return self._json(
            200,
            {"ok": True, "stopped": stopped, "project": project},
        )

    def _h_projects_add(self) -> None:
        rt = self.runtime
        data = self._read_body()
        raw = str(data.get("path", "")).strip()
        root = rt._valid_root(raw)
        if root is None:
            return self._json(400, {"error": f"not a readable folder: {raw}"})
        preview_url = str(data.get("previewUrl", "") or "").strip().rstrip("/")
        if preview_url and not rt._valid_target(preview_url):
            return self._json(
                400,
                {
                    "error": "dev server URL must be http://localhost:PORT or "
                    "http://127.0.0.1:PORT"
                },
            )

        def existing_project() -> dict[str, Any] | None:
            for item in rt._CFG["projects"]:
                if str(rt.Path(item["path"]).resolve()) == str(root):
                    return item
            return None

        # Scan/update is a single registry transaction.
        with rt._QUEUE_LOCK:
            project = existing_project()
            if project is not None:
                if preview_url and project.get("previewUrl", "") != preview_url:
                    project["previewUrl"] = preview_url
                    rt._save_cfg(rt._CFG)
                    return self._json(
                        200,
                        {
                            "ok": True,
                            "project": project,
                            "existing": True,
                            "updated": "previewUrl",
                        },
                    )
                return self._json(
                    200,
                    {"ok": True, "project": project, "existing": True},
                )

        project = {
            "id": rt.uuid.uuid4().hex[:8],
            "path": str(root),
            "name": root.name,
        }
        # Discovery invokes lsof/probes and must stay outside the registry lock.
        detected = []
        if preview_url:
            project["previewUrl"] = preview_url
        else:
            detected = rt._detect_dev_servers(root)
            html = [candidate for candidate in detected if candidate["servesHtml"]]
            if len(html) == 1:
                project["previewUrl"] = html[0]["url"]

        with rt._QUEUE_LOCK:
            duplicate = existing_project()
            if duplicate is not None:
                return self._json(
                    200,
                    {"ok": True, "project": duplicate, "existing": True},
                )
            rt._CFG["projects"].append(project)
            rt._save_cfg(rt._CFG)
        return self._json(
            200,
            {
                "ok": True,
                "project": project,
                "detected": detected,
                "autoDetected": bool(not preview_url and project.get("previewUrl")),
            },
        )

    def _h_detect_dev_server(self, query: dict[str, list[str]]) -> None:
        rt = self.runtime
        project_id = (query.get("id") or [""])[0]
        raw = (query.get("path") or [""])[0]
        if project_id:
            project = next(
                (item for item in rt._CFG["projects"] if item["id"] == project_id),
                None,
            )
            if project is None:
                return self._json(404, {"error": "project not found"})
            raw = project["path"]
        root = rt._valid_root(raw)
        if root is None:
            return self._json(400, {"error": f"not a readable folder: {raw}"})
        candidates = rt._detect_dev_servers(root)
        html = [candidate for candidate in candidates if candidate["servesHtml"]]
        return self._json(
            200,
            {
                "ok": True,
                "root": str(root),
                "candidates": candidates,
                "suggested": html[0]["url"] if len(html) == 1 else "",
            },
        )

    def _h_projects_preview_url(self) -> None:
        rt = self.runtime
        data = self._read_body()
        project_id = str(data.get("id", ""))
        url = str(data.get("previewUrl", "") or "").strip().rstrip("/")
        if url and not rt._valid_target(url):
            return self._json(
                400,
                {
                    "error": "dev server URL must be http://localhost:PORT or "
                    "http://127.0.0.1:PORT"
                },
            )
        with rt._QUEUE_LOCK:
            project = next(
                (item for item in rt._CFG["projects"] if item["id"] == project_id),
                None,
            )
            if project is None:
                return self._json(404, {"error": "project not found"})
            if url:
                project["previewUrl"] = url
            else:
                project.pop("previewUrl", None)
            rt._save_cfg(rt._CFG)
        return self._json(200, {"ok": True, "project": project})

    def _h_projects_select(self) -> None:
        rt = self.runtime
        data = self._read_body()
        project_id = str(data.get("id", ""))
        with rt._QUEUE_LOCK:
            project = next(
                (item for item in rt._CFG["projects"] if item["id"] == project_id),
                None,
            )
            if project is None:
                return self._json(404, {"error": "project not found"})
            root = rt._valid_root(project["path"])
            if root is None:
                return self._json(
                    400,
                    {"error": f"folder no longer readable: {project['path']}"},
                )
            rt._ROOT = str(root)
            rt._TARGET = ""
            rt._CFG["activeId"] = project_id
            rt._save_cfg(rt._CFG)
        return self._json(200, {"ok": True, "project": project})

    def _h_projects_remove(self) -> None:
        rt = self.runtime
        data = self._read_body()
        project_id = str(data.get("id", ""))
        _QUEUE_LOCK = rt._QUEUE_LOCK
        with _QUEUE_LOCK:
            project = next(
                (item for item in rt._CFG["projects"] if item["id"] == project_id),
                None,
            )
            if project is None:
                return self._json(404, {"error": "project not found"})
            rt._CFG["projects"] = [item for item in rt._CFG["projects"] if item["id"] != project_id]
            if rt._CFG.get("activeId") == project_id:
                rt._CFG["activeId"] = ""
                rt._ROOT = ""
            rt._save_cfg(rt._CFG)

        # Teardown can wait on processes and accept loops; do it after releasing
        # the registry lock, before forgetting the final resource handles.
        _stop_dev_proc = rt._stop_dev_proc
        _stop_static_preview = rt._stop_static_preview
        _stop_dev_proc(project_id)
        _stop_static_preview(project_id)
        return self._json(200, {"ok": True, "id": project_id})

    def _h_pick_folder(self) -> None:
        """Native picker seam implemented by the bound ``server.Handler``."""

        raise NotImplementedError("the bound handler must provide the native picker")

    def _h_thread(self, query: dict[str, list[str]]) -> None:
        """Append progress to one comment thread or the request-level thread."""

        rt = self.runtime
        request_id = (query.get("id") or [""])[0]
        comment_id = (query.get("cid") or [""])[0]
        if not request_id or not rt._ID_RE.match(request_id):
            return self._json(400, {"error": "valid id required"})
        if comment_id and not rt._ID_RE.match(comment_id):
            return self._json(400, {"error": "invalid cid"})
        data = self._read_body()
        # Redact before persistence.  Security-policy ordering remains owned by
        # server and is reached through this single late-bound seam.
        text = rt._redact_incoming_thread_text(str(data.get("text", "")).strip())
        role = str(data.get("role", "agent")).strip() or "agent"
        if role not in ("agent", "user", "system"):
            role = "agent"
        new_status = str(data.get("status", "")).strip()
        if not text and not new_status:
            return self._json(400, {"error": "text or status required"})

        def transaction() -> tuple[int, dict[str, Any]]:
            path = rt._find_request(request_id)
            if path is None:
                return 404, {"error": "not found"}
            request = rt._read_request(path)
            if request is None:
                return 500, {"error": "request unreadable"}

            entry = {"role": role, "text": text, "ts": rt._now_iso()}
            if comment_id:
                target = next(
                    (
                        comment
                        for comment in (request.get("comments") or [])
                        if comment.get("cid") == comment_id
                    ),
                    None,
                )
                if target is None:
                    return 404, {"error": f"comment {comment_id} not in request {request_id}"}
                thread = target.get("thread")
                if not isinstance(thread, list):
                    thread = []
                if text:
                    if len(thread) >= rt.MAX_THREAD_ENTRIES:
                        return 429, {
                            "error": (
                                f"comment {comment_id} already holds "
                                f"{rt.MAX_THREAD_ENTRIES} thread entries — the "
                                "conversation is full"
                            ),
                            "code": "thread_entry_limit",
                        }
                    thread.append(entry)
                target["thread"] = thread
                if new_status in rt._COMMENT_STATUSES:
                    target["status"] = new_status
            else:
                thread = request.get("thread")
                if not isinstance(thread, list):
                    thread = []
                if text:
                    if len(thread) >= rt.MAX_THREAD_ENTRIES:
                        return 429, {
                            "error": (
                                f"request {request_id} already holds "
                                f"{rt.MAX_THREAD_ENTRIES} thread entries — the "
                                "conversation is full"
                            ),
                            "code": "thread_entry_limit",
                        }
                    thread.append(entry)
                request["thread"] = thread
                if new_status == "done":
                    for comment in request.get("comments") or []:
                        comment["status"] = "done"

            agent_activity = role in ("agent", "system")
            worked = any(
                comment.get("status") != "new" for comment in (request.get("comments") or [])
            )
            if request.get("state") not in ("draft", "sent") or (
                request.get("state") == "draft" and (worked or agent_activity)
            ):
                request["state"] = "sent"
                if not request.get("sentAt"):
                    request["sentAt"] = rt._now_iso()

            try:
                rt._write_request(path, request)
            except OSError as exc:
                return 500, {"error": str(exc)}
            return 200, {
                "ok": True,
                "id": request_id,
                "cid": comment_id,
                "status": rt._request_status(request),
                "request": rt._summarize(request),
            }

        with rt._QUEUE_LOCK:
            try:
                code, response = transaction()
            except rt._RecordTooLarge as exc:
                code = 413
                response = {"error": str(exc), "code": "record_too_large"}
        return self._json(code, response)

    def _h_set_source(self) -> None:
        """Set a legacy folder or loopback dev-server preview source."""

        rt = self.runtime
        data = self._read_body()
        value = str(data.get("value", data.get("url", ""))).strip()
        if not value:
            rt._ROOT = ""
            rt._TARGET = ""
            return self._json(
                200,
                {"ok": True, "mode": "cleared", "proxyUrl": rt.PROXY_PUBLIC_BASE},
            )
        if value.lower().startswith(("http://", "https://")):
            if not rt._valid_target(value):
                return self._json(
                    400,
                    {"error": "URL must be http://localhost:PORT or " "http://127.0.0.1:PORT"},
                )
            rt._TARGET = value.rstrip("/")
            rt._ROOT = ""
            return self._json(
                200,
                {
                    "ok": True,
                    "mode": "url",
                    "target": rt._TARGET,
                    "proxyUrl": rt.PROXY_PUBLIC_BASE,
                },
            )
        root = rt._valid_root(value)
        if root is None:
            return self._json(400, {"error": f"not a readable folder: {value}"})
        rt._ROOT = str(root)
        rt._TARGET = ""
        return self._json(
            200,
            {
                "ok": True,
                "mode": "folder",
                "root": rt._ROOT,
                "proxyUrl": rt.PROXY_PUBLIC_BASE,
            },
        )

    def _h_inject(self) -> None:
        rt = self.runtime
        try:
            javascript = rt.INJECT_FILE.read_bytes()
        except OSError:
            return self._send_raw(
                404,
                "application/javascript",
                b"// overlay not found",
            )
        return self._send_raw(
            200,
            "application/javascript; charset=utf-8",
            javascript,
        )

    def _send_raw(self, code: int, content_type: str, body: bytes) -> None:
        rt = self.runtime
        self.send_response(code)
        self.send_header("Content-Type", rt._header_value(content_type))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        rt = self.runtime
        body = rt.json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


__all__ = ["Handler", "IncompleteBody"]
