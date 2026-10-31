"""The pool of running language servers and the four requests the dashboard makes.

One server runs per (family, project root), started on the first request for a
file under that root and stopped after it has been idle for
``IDLE_SHUTDOWN_SECS`` or when the pool needs its slot. Every public method
blocks; the app's routes call them on :func:`pools.request_executor`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict, deque

from kiro_crew.apps.builtins.code_intel import classfiles
from kiro_crew.apps.builtins.code_intel.client import RETRYABLE_RPC_CODES, LspClient, LspError
from kiro_crew.apps.builtins.code_intel.protocol import (
    flatten_symbols,
    hover_markdown,
    locations_of,
    symbol_kind_name,
    uri_to_path,
)
from kiro_crew.apps.builtins.code_intel.servers import (
    ServerSpec,
    find_project_root,
    initialization_options,
    install_hint,
    language_id,
    missing_references_hint,
    project_folders,
    resolve_argv,
    setup_hint,
    spec_by_id,
    spec_for_path,
)

logger = logging.getLogger(__name__)

# Most servers that may run at once; the least recently used one is stopped
# to make room. Language servers commonly hold hundreds of MB each.
MAX_SERVERS = 4
# A server unused this long is stopped.
IDLE_SHUTDOWN_SECS = 15 * 60
# How long a request waits for a server's ``initialize`` before answering
# "starting" (the handshake carries on in the background).
READY_WAIT_SECS = 20.0
# Ceiling on the ``initialize`` handshake itself; JVM servers index first.
INIT_TIMEOUT_SECS = 180.0
# Per-request ceiling once the server is ready.
REQUEST_TIMEOUT_SECS = 20.0
# A server that failed to start is not retried for this long.
FAILED_START_BACKOFF_SECS = 60.0
# For this long after start, an empty definition/references answer is retried
# once a second: servers answer empty while their first index is still building.
WARMUP_RETRY_SECS = 30.0
# Largest file the gateway opens on a server when the caller sends no text.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
# Cap on locations returned (references can be workspace-wide).
MAX_LOCATIONS = 500
# Cap on outline entries.
MAX_SYMBOLS = 2000
# Characters of each location's source line returned as its preview.
PREVIEW_CHARS = 240
# Distinct files read for previews in one answer.
MAX_PREVIEW_FILES = 100

OPERATIONS = ("definition", "references", "hover", "symbols", "status")
# Cap on workspace-symbol search results.
MAX_WORKSPACE_SYMBOLS = 500
# Project detection walks at most this many directory entries ...
DETECT_MAX_ENTRIES = 200_000
# ... this deep below the directory it was asked about (Java's package
# directories alone sit eight or more levels under a project root) ...
DETECT_MAX_DEPTH = 24
# ... and reports at most this many (server, root) pairs.
DETECT_MAX_PROJECTS = 200
# Directories detection never descends into: dependencies, build output, caches.
# `bin` is Eclipse's (and bemol's) compiled-class output beside every Java source tree.
DETECT_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "build",
        "dist",
        "out",
        "target",
        "bin",
        "venv",
        "env",
        "__pycache__",
        "vendor",
    }
)


class CodeIntelError(Exception):
    """A request the manager could not answer, with the code the HTTP layer returns."""

    def __init__(self, message: str, code: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def _read_text(path: str) -> str:
    from kiro_crew.hooks import safe_read_prefix

    data = safe_read_prefix(path, MAX_DOCUMENT_BYTES + 1)
    if data is None:
        raise CodeIntelError("File is not readable", "not_readable")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise CodeIntelError("File is too large for code intelligence", "too_large")
    return data.decode("utf-8", "replace")


class LspManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: OrderedDict[tuple[str, str], LspClient] = OrderedDict()
        # (server, root) -> (when, error, stderr tail) of a recent failed start.
        self._failed: dict[tuple[str, str], tuple[float, str, str]] = {}
        self._closed = False

    # ── pool ───────────────────────────────────────────────────────────────

    def _client_for(self, spec: ServerSpec, root: str) -> LspClient:
        key = (spec.id, root)
        stopping: list[LspClient] = []
        try:
            with self._lock:
                if self._closed:
                    raise CodeIntelError("Code intelligence is shutting down", "shutting_down")
                now = time.monotonic()
                self._sweep_locked()
                client = self._clients.get(key) or self._covering(spec.id, root)
                if client is not None:
                    self._clients.move_to_end((client.server_id, client.root))
                    client.last_used = now
                    return client
                failed = self._failed.get(key)
                if failed is not None and now - failed[0] < FAILED_START_BACKOFF_SECS:
                    raise CodeIntelError(
                        f"The {spec.id} language server failed to start: {failed[1]}",
                        "server_failed",
                    )
                refusals: list[str] = []
                argv = resolve_argv(spec, root, refusals)
                if argv is None:
                    raise _no_server(spec, refusals)
                while len(self._clients) >= MAX_SERVERS:
                    _, oldest = self._clients.popitem(last=False)
                    stopping.append(oldest)
                folders, missing = project_folders(spec, root)
                client = LspClient(
                    spec.id, root, argv, initialization_options(spec, root, folders), folders
                )
                client.setup_hint = setup_hint(spec, root) or missing_references_hint(missing)
                self._clients[key] = client
                self._failed.pop(key, None)
                threading.Thread(
                    target=self._start,
                    args=(key, client),
                    name=f"lsp-start-{spec.id}",
                    daemon=True,
                ).start()
                return client
        finally:
            for old in stopping:
                threading.Thread(target=old.shutdown, name="lsp-stop", daemon=True).start()

    def _running_client(self, spec: ServerSpec, root: str) -> LspClient:
        """The server already running for (*spec*, *root*), never a new one.

        Editor navigation (Cmd-click, F12, the underline) asks here: a language
        server is started only from the LSP tab, where the user picks the project
        it indexes. A server still starting counts as running; one that failed
        says why; none at all is ``not_running``, which the viewer turns into a
        pointer at the LSP tab.
        """
        key = (spec.id, root)
        with self._lock:
            if self._closed:
                raise CodeIntelError("Code intelligence is shutting down", "shutting_down")
            self._sweep_locked()
            client = self._clients.get(key) or self._covering(spec.id, root)
            if client is not None:
                self._clients.move_to_end((client.server_id, client.root))
                client.last_used = time.monotonic()
                return client
            failed = self._failed.get(key)
        if failed is not None and time.monotonic() - failed[0] < FAILED_START_BACKOFF_SECS:
            raise CodeIntelError(
                f"The {spec.id} language server failed to start: {failed[1]}",
                "server_failed",
                server=spec.id,
                root=root,
            )
        raise CodeIntelError(
            f"No {spec.id} language server is running for this project",
            "not_running",
            server=spec.id,
            root=root,
        )

    def _sweep_locked(self) -> None:
        """Drop dead and idle servers from the pool; stop the idle ones. Lock held."""
        now = time.monotonic()
        stopping: list[LspClient] = []
        for other_key, other in list(self._clients.items()):
            if other.start_error is not None:
                self._failed.setdefault(other_key, (now, other.start_error, other.stderr_tail()))
            if not other.alive or now - other.last_used > IDLE_SHUTDOWN_SECS:
                stopping.append(self._clients.pop(other_key))
        for old in stopping:
            threading.Thread(target=old.shutdown, name="lsp-stop", daemon=True).start()

    def _covering(self, server_id: str, root: str) -> LspClient | None:
        """A live server that already opened *root* among its referenced projects.

        A file in a package that another package depends on is answered by the
        server already holding both, rather than a second server that would index
        the same project again. Its answers can also reach into the projects that
        depend on *root*, which a references query wants anyway. Call with the
        lock held.
        """
        for client in reversed(self._clients.values()):
            if client.server_id == server_id and client.alive and root in client.folders[1:]:
                return client
        return None

    def _start(self, key: tuple[str, str], client: LspClient) -> None:
        client.start(INIT_TIMEOUT_SECS)
        try:
            client.wait_ready(0)
        except LspError as exc:
            with self._lock:
                if self._clients.get(key) is client:
                    del self._clients[key]
                self._failed[key] = (time.monotonic(), str(exc), client.stderr_tail())

    def running(self) -> list[dict]:
        with self._lock:
            return [
                {"server": c.server_id, "root": c.root, "ready": c.started_at > 0}
                for c in self._clients.values()
                if c.alive
            ]

    def servers(self) -> dict:
        """Every server in the pool and every recent failed start, for the LSP tab."""
        now = time.monotonic()
        with self._lock:
            clients = list(self._clients.values())
            failed = dict(self._failed)
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for client in clients:
            key = (client.server_id, client.root)
            seen.add(key)
            error = client.start_error
            if error is not None:
                state = "failed"
            elif not client.alive:
                state = "exited"
            elif client.started_at > 0:
                state = "ready"
            else:
                state = "starting"
            out.append(
                _server_entry(
                    client.server_id,
                    client.root,
                    state,
                    error=error or "",
                    stderr=client.stderr_tail(),
                    notices=_hinted(client, client.notices()),
                    pid=client.pid if state in ("starting", "ready") else None,
                    command=client.argv[0] if client.argv else "",
                    uptime=now - client.started_at if client.started_at > 0 else None,
                    age=now - client.spawned_at,
                    idle=now - client.last_used,
                    folders=client.folders[1:],
                )
            )
        for (server_id, root), (when, error, stderr) in failed.items():
            if (server_id, root) in seen:
                continue
            out.append(
                _server_entry(
                    server_id,
                    root,
                    "failed",
                    error=error,
                    stderr=stderr,
                    age=now - when,
                    retry_in=max(0.0, FAILED_START_BACKOFF_SECS - (now - when)),
                )
            )
        out.sort(key=lambda item: (item["root"], item["server"]))
        return {
            "servers": out,
            "maxServers": MAX_SERVERS,
            "idleShutdownSecs": IDLE_SHUTDOWN_SECS,
        }

    def stop(self, server_id: str, root: str) -> bool:
        """Stop the server for (*server_id*, *root*) and forget a failed start.

        True when there was anything to stop or forget.
        """
        key = (server_id, root)
        with self._lock:
            client = self._clients.pop(key, None)
            forgot = self._failed.pop(key, None) is not None
        if client is not None:
            client.shutdown()
        return client is not None or forgot

    def start(self, server_id: str, root: str) -> None:
        """Start the server for (*server_id*, *root*); nothing when one is running.

        The LSP tab's Start button, and the only way a server is started: a file
        request never starts one.
        """
        spec = self._spec_at_root(server_id, root)
        self._client_for(spec, os.path.realpath(root))

    def restart(self, server_id: str, root: str) -> None:
        """Stop the server for (*server_id*, *root*), then start a fresh one."""
        spec = self._spec_at_root(server_id, root)
        root = os.path.realpath(root)
        self.stop(server_id, root)
        self._client_for(spec, root)

    def _spec_at_root(self, server_id: str, root: str) -> ServerSpec:
        """The spec for *server_id*, refusing a *root* the server would not be started in.

        A root must be the one :func:`find_project_root` gives for a file directly
        in it: a directory inside a marked project is refused, since its server
        belongs at the project's root. Any other directory is the reader's choice,
        their home or a filesystem root included.
        """
        spec = spec_by_id(server_id)
        if spec is None:
            raise CodeIntelError("Unknown language server", "bad_request")
        root = os.path.realpath(root)
        if find_project_root(os.path.join(root, "_"), spec) != root:
            raise CodeIntelError("Not a project root for this language server", "bad_request")
        return spec

    # ── project-wide ───────────────────────────────────────────────────────

    def detect(self, directory: str) -> dict:
        """The languages under *directory*, each with its project root and server.

        Walks the tree (bounded by ``DETECT_MAX_*``, skipping hidden and build
        directories), groups files by (family, project root), and reports per
        group whether a server is installed, refused, or missing, and whether one
        is running. Nothing is started.
        """
        from kiro_crew.security.paths import is_sensitive_resolved_path, path_contains_sensitive

        directory = os.path.realpath(directory)
        # The walk follows no link, so every directory it reaches is canonical and
        # under *directory*. When no fenced location lies under *directory* at all
        # (one list comparison), none of them can be one; otherwise each is asked.
        fenced = path_contains_sensitive(directory, pre_resolved=True)
        counts: dict[tuple[str, str], int] = {}
        # (family, directory) -> project root: every file in one directory has the same.
        root_cache: dict[tuple[str, str], str] = {}

        entries = 0
        truncated = False
        # Breadth-first, so a walk cut off by a bound has covered the shallow
        # project roots rather than the first deep subtree it entered.
        queue: deque[tuple[str, int]] = deque([(directory, 0)])
        while queue and not truncated:
            current, depth = queue.popleft()
            try:
                with os.scandir(current) as it:
                    listing = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for entry in listing:
                entries += 1
                if entries > DETECT_MAX_ENTRIES:
                    truncated = True
                    break
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if (
                            depth + 1 <= DETECT_MAX_DEPTH
                            and name not in DETECT_SKIP_DIRS
                            and not (fenced and is_sensitive_resolved_path(entry.path))
                        ):
                            queue.append((entry.path, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                spec = spec_for_path(name)
                if spec is None:
                    continue
                root = root_cache.get((spec.id, current))
                if root is None:
                    root = find_project_root(entry.path, spec)
                    root_cache[(spec.id, current)] = root
                key = (spec.id, root)
                if key not in counts and len(counts) >= DETECT_MAX_PROJECTS:
                    truncated = True
                    continue
                counts[key] = counts.get(key, 0) + 1
        with self._lock:
            states = {
                key: ("failed" if c.start_error else "ready" if c.started_at > 0 else "starting")
                for key, c in self._clients.items()
                if c.alive or c.start_error
            }
            failed = set(self._failed)
        argv_cache: dict[tuple[str, str], tuple[list[str] | None, list[str]]] = {}
        projects: list[dict] = []
        for (server_id, root), files in sorted(counts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            spec = spec_by_id(server_id)
            assert spec is not None
            if (server_id, root) not in argv_cache:
                refusals: list[str] = []
                argv_cache[(server_id, root)] = (
                    resolve_argv(spec, root, refusals, create=False),
                    refusals,
                )
            argv, refusals = argv_cache[(server_id, root)]
            state = states.get((server_id, root)) or (
                "failed" if (server_id, root) in failed else "stopped"
            )
            projects.append(
                {
                    "server": server_id,
                    "root": root,
                    "files": files,
                    "available": argv is not None,
                    "command": argv[0] if argv else "",
                    "install": [] if argv else install_hint(spec),
                    "refused": refusals,
                    "state": state,
                }
            )
        return {"directory": directory, "projects": projects, "truncated": truncated}

    def workspace_symbols(self, server_id: str, root: str, query: str) -> dict:
        """``workspace/symbol`` for *query* on the server for (*server_id*, *root*).

        Starts the server when none is running: the LSP tab asked for this root.
        """
        spec = self._spec_at_root(server_id, root)
        root = os.path.realpath(root)
        client = self._client_for(spec, root)
        try:
            if not client.wait_ready(READY_WAIT_SECS):
                raise CodeIntelError(
                    f"The {spec.id} language server is still starting",
                    "server_starting",
                    server=spec.id,
                )
            raw = client.request("workspace/symbol", {"query": query}, timeout=REQUEST_TIMEOUT_SECS)
        except LspError as exc:
            raise CodeIntelError(str(exc), exc.code, server=spec.id) from exc
        client.last_used = time.monotonic()
        symbols, truncated, external, hidden = _workspace_symbol_answer(raw, client)
        return {
            "symbols": symbols,
            "truncated": truncated,
            "external": external,
            "hidden": hidden,
            "server": spec.id,
            "root": root,
        }

    def workspace_symbols_all(self, query: str) -> dict:
        """``workspace/symbol`` for *query* on every running server, merged.

        Starts nothing: only servers that have finished starting are asked, in
        parallel. Each symbol names the server and root that found it, and a
        server that fails is reported in ``errors`` without failing the rest.
        """
        from concurrent.futures import ThreadPoolExecutor

        with self._lock:
            clients = [
                c
                for c in self._clients.values()
                if c.alive and c.started_at > 0 and c.start_error is None
            ]

        def ask(client: LspClient) -> object:
            return client.request(
                "workspace/symbol", {"query": query}, timeout=REQUEST_TIMEOUT_SECS
            )

        symbols: list[dict] = []
        errors: list[dict] = []
        external = hidden = 0
        truncated = False
        if clients:
            with ThreadPoolExecutor(
                max_workers=len(clients), thread_name_prefix="lsp-wsym"
            ) as pool:
                futures = [(c, pool.submit(ask, c)) for c in clients]
                for client, future in futures:
                    try:
                        raw = future.result()
                    except LspError as exc:
                        from kiro_crew.security.redaction import redact_credentials

                        errors.append(
                            {
                                "server": client.server_id,
                                "root": client.root,
                                "error": redact_credentials(str(exc))[0],
                            }
                        )
                        continue
                    client.last_used = time.monotonic()
                    found, cut, ext, hid = _workspace_symbol_answer(raw, client)
                    truncated = truncated or cut
                    external += ext
                    hidden += hid
                    symbols.extend(
                        {**s, "server": client.server_id, "root": client.root} for s in found
                    )
        # One symbol reached through two servers (a project another opened as a
        # reference) is listed once; closer name matches come first.
        unique: dict[tuple, dict] = {}
        for sym in symbols:
            unique.setdefault((sym["path"] or sym.get("uri", ""), sym["line"], sym["name"]), sym)
        needle = query.lower()
        merged = sorted(
            unique.values(),
            key=lambda s: (
                s["name"].lower() != needle,
                not s["name"].lower().startswith(needle),
                not s["path"],
                s["name"].lower(),
                s["path"],
            ),
        )
        if len(merged) > MAX_WORKSPACE_SYMBOLS:
            merged, truncated = merged[:MAX_WORKSPACE_SYMBOLS], True
        return {
            "symbols": merged,
            "truncated": truncated,
            "external": external,
            "hidden": hidden,
            "servers": len(clients),
            "errors": errors,
        }

    def open_class(self, server_id: str, root: str, uri: str) -> dict:
        """Write out library class *uri* from the running server at *root*; ``{path}``.

        For a workspace symbol found in a jar: its source is fetched only when it
        is opened. Starts nothing.
        """
        if not classfiles.is_class_uri(uri):
            raise CodeIntelError("Not a library class", "bad_request")
        root = os.path.realpath(root)
        with self._lock:
            client = self._clients.get((server_id, root))
        if client is None or not client.alive or client.started_at <= 0:
            raise CodeIntelError("That language server is not running", "not_running")
        # One class the user asked for: the full request timeout, not a share of a budget.
        path = _class_file(client, uri, time.monotonic() + REQUEST_TIMEOUT_SECS)
        if path is None:
            raise CodeIntelError("The server has no source for that class", "no_source")
        client.last_used = time.monotonic()
        return {"path": path}

    def shutdown_all(self) -> None:
        with self._lock:
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.shutdown()

    # ── requests ───────────────────────────────────────────────────────────

    def handle(
        self,
        operation: str,
        path: str,
        line: int,
        character: int,
        text: str | None,
        text_sha: str | None = None,
    ) -> dict:
        """Answer one dashboard request for the already-validated *path*.

        *line* and *character* are 0-based, *character* in UTF-16 code units (the
        dashboard's own string indexing, and LSP's default encoding).

        The buffer comes as *text*, or as *text_sha*, the SHA-256 of its UTF-8
        bytes: then the copy the server already holds is used when it matches,
        else the file on disk when that matches, else ``text_required`` asks the
        viewer to send the text. With neither, the file on disk is used.
        """
        if operation not in OPERATIONS:
            raise CodeIntelError("Unknown operation", "bad_request")
        # The server's workspace is a canonical path, so the document must be too:
        # a file opened under a symlinked spelling is outside the workspace to it.
        path = os.path.realpath(path)
        spec = spec_for_path(path)
        if spec is None:
            raise CodeIntelError("No language server handles this file type", "unsupported")
        # A library class written out from a jdt:// answer: ask the server that
        # produced it, about the class itself rather than the copy on disk.
        class_origin = classfiles.origin(path) if spec.id == "java" else None
        root = class_origin[1] if class_origin else find_project_root(path, spec)
        if operation == "status":
            # A probe: it creates no data directory, as `detect` does not.
            refusals: list[str] = []
            argv = resolve_argv(spec, root, refusals, create=False)
            return {
                "server": spec.id,
                "root": root,
                "available": argv is not None,
                "command": argv[0] if argv else "",
                "install": [] if argv else install_hint(spec),
                "refused": refusals,
            }
        client = self._running_client(spec, root)
        try:
            if not client.wait_ready(READY_WAIT_SECS):
                raise CodeIntelError(
                    f"The {spec.id} language server is still starting",
                    "server_starting",
                    server=spec.id,
                )
        except LspError as exc:
            raise CodeIntelError(str(exc), exc.code, server=spec.id) from exc
        try:
            if class_origin:
                uri = class_origin[0]
            else:
                body = text if text is not None else _known_text(client, path, text_sha)
                uri = client.sync_document(path, language_id(spec, path), body)
            result = self._request(client, operation, uri, line, character)
        except LspError as exc:
            raise CodeIntelError(str(exc), exc.code, server=spec.id) from exc
        client.last_used = time.monotonic()
        result["server"] = spec.id
        result["root"] = root
        # An empty answer from a server that reported an error usually means it
        # never loaded the project: say what it said rather than "nothing found".
        if _is_empty(result):
            result["notices"] = _redacted_notices(client)
        if client.setup_hint:
            result["hint"] = client.setup_hint
        # The canonical spelling of the requested file, so the viewer can tell a
        # location in this file from one in another under a different spelling.
        result["document"] = path
        return result

    def _request(
        self, client: LspClient, operation: str, uri: str, line: int, character: int
    ) -> dict:
        document = {"textDocument": {"uri": uri}}
        position = {**document, "position": {"line": line, "character": character}}
        if operation == "symbols":
            raw = client.request(
                "textDocument/documentSymbol", document, timeout=REQUEST_TIMEOUT_SECS
            )
            symbols, truncated = flatten_symbols(raw, MAX_SYMBOLS)
            return {"symbols": symbols, "truncated": truncated}
        if operation == "hover":
            raw = client.request("textDocument/hover", position, timeout=REQUEST_TIMEOUT_SECS)
            return {"contents": hover_markdown(raw)}
        if operation == "definition":
            method, params = "textDocument/definition", position
        else:
            method = "textDocument/references"
            params = {**position, "context": {"includeDeclaration": True}}
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECS
        while True:
            out_of_time = time.monotonic() + 1.0 >= deadline
            try:
                raw = client.request(method, params, timeout=max(1.0, deadline - time.monotonic()))
            except LspError as exc:
                if exc.rpc_code not in RETRYABLE_RPC_CODES or out_of_time:
                    raise
                time.sleep(1.0)
                continue
            locations = _with_class_files(client, locations_of(raw))
            warming = time.monotonic() - client.started_at < WARMUP_RETRY_SECS
            if locations or not warming or out_of_time:
                break
            time.sleep(1.0)
        return _location_answer(locations)


# Library classes written out per answer; past this they stay counted as external.
MAX_CLASS_FILES_PER_ANSWER = 20
# ... and the time spent fetching them all: one slow decompile must not hold
# the whole answer for its own request timeout, twenty times over.
CLASS_FILES_BUDGET_SECS = 8.0


def _with_class_files(
    client: LspClient, locations: list[tuple[str, tuple[int, int, int, int]]]
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Locations with each ``jdt://`` class replaced by the file its source is in.

    jdtls serves a library class's source through ``java/classFileContents``;
    it is written out once (:mod:`classfiles`) so the editor can open it. A class
    the server has no source for stays a ``jdt://`` URI, counted as external.
    """
    from kiro_crew.apps.builtins.code_intel.protocol import path_to_uri

    written: dict[str, str | None] = {}
    deadline = time.monotonic() + CLASS_FILES_BUDGET_SECS
    out = []
    for uri, rng in locations:
        if classfiles.is_class_uri(uri):
            if uri not in written and len(written) < MAX_CLASS_FILES_PER_ANSWER:
                written[uri] = _class_file(client, uri, deadline)
            path = written.get(uri)
            if path:
                uri = path_to_uri(path)
        out.append((uri, rng))
    return out


def _class_file(client: LspClient, uri: str, deadline: float) -> str | None:
    path = classfiles.path_for(uri)
    if os.path.isfile(path) and classfiles.origin(os.path.realpath(path)):
        return path
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None  # out of budget for this answer: stays a class URI, counted external
    try:
        contents = client.request("java/classFileContents", {"uri": uri}, timeout=remaining)
    except LspError as exc:
        logger.info("code_intel: no source for %s: %s", uri, exc)
        return None
    if not isinstance(contents, str):
        return None
    try:
        return classfiles.write(uri, client.root, contents)
    except OSError as exc:
        logger.warning("code_intel: could not write the source of %s: %s", uri, exc)
        return None


def _known_text(client: LspClient, path: str, text_sha: str | None) -> str:
    """The buffer a ``text_sha`` request names, from the server's copy or the disk."""
    if text_sha is None:
        return _read_text(path)
    held = client.document_text(path)
    if held is not None and _sha(held) == text_sha:
        return held
    on_disk = _read_text(path)
    if _sha(on_disk) == text_sha:
        return on_disk
    raise CodeIntelError("The server needs the editor's text", "text_required")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _is_empty(result: dict) -> bool:
    for key in ("locations", "symbols"):
        if key in result:
            return not result[key]
    return "contents" in result and not result["contents"]


def _hinted(client: LspClient, notices: list[dict]) -> list[dict]:
    if client.setup_hint:
        return [*notices, {"level": "warning", "message": client.setup_hint}]
    return notices


def _redacted_notices(client: LspClient) -> list[dict]:
    from kiro_crew.security.redaction import redact_credentials

    return [
        {"level": n["level"], "message": redact_credentials(n["message"])[0]}
        for n in client.notices()
        if n["level"] == "error"
    ]


def _server_entry(
    server_id: str,
    root: str,
    state: str,
    *,
    error: str = "",
    stderr: str = "",
    pid: int | None = None,
    command: str = "",
    uptime: float | None = None,
    age: float = 0.0,
    idle: float | None = None,
    retry_in: float | None = None,
    notices: list[dict] | None = None,
    folders: list[str] | None = None,
) -> dict:
    from kiro_crew.security.redaction import redact_credentials

    return {
        "server": server_id,
        "root": root,
        # The projects opened beside the root because it references them.
        "folders": list(folders or []),
        "state": state,
        "error": redact_credentials(error)[0] if error else "",
        # A server's stderr can quote its environment or arguments: credentials
        # in it are redacted before it leaves the gateway.
        "stderr": redact_credentials(stderr)[0] if stderr else "",
        "pid": pid,
        "command": command,
        "uptimeSecs": None if uptime is None else round(uptime),
        "ageSecs": round(age),
        "idleSecs": None if idle is None else round(idle),
        "shutdownInSecs": None if idle is None else max(0, round(IDLE_SHUTDOWN_SECS - idle)),
        "retryInSecs": None if retry_in is None else round(retry_in),
        "notices": [
            {"level": n["level"], "message": redact_credentials(n["message"])[0]}
            for n in notices or []
        ],
    }


def _workspace_symbol_answer(
    raw: object, client: LspClient | None = None
) -> tuple[list[dict], bool, int, int]:
    """``SymbolInformation[]`` / ``WorkspaceSymbol[]`` as ``{name, kind, container, path, line, character}``.

    Lines are 1-based. A symbol in a library class (a ``jdt://`` URI) is kept
    with ``path`` empty and its ``uri``, for :meth:`LspManager.open_class` to
    write out when it is opened; any other symbol with no local file (a URI the
    server resolves lazily) is counted in ``external``, and one in a credential
    store in ``hidden``.
    """
    from kiro_crew.security import is_sensitive_path

    out: list[dict] = []
    external = hidden = 0
    truncated = False
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        location = item.get("location")
        if not isinstance(location, dict):
            continue
        uri = str(location.get("uri") or "")
        path = uri_to_path(uri)
        library = path is None and classfiles.is_class_uri(uri) and client is not None
        if path is None and not library:
            external += 1
            continue
        if path is not None and is_sensitive_path(path):
            hidden += 1
            continue
        start = (location.get("range") or {}).get("start") or {}
        if len(out) >= MAX_WORKSPACE_SYMBOLS:
            truncated = True
            break
        line = start.get("line")
        character = start.get("character")
        out.append(
            {
                "name": str(item.get("name") or ""),
                "kind": symbol_kind_name(item.get("kind")),
                "container": str(item.get("containerName") or ""),
                "path": path or "",
                **({"uri": uri} if library else {}),
                "line": line + 1 if isinstance(line, int) else 1,
                "character": character if isinstance(character, int) else 0,
            }
        )
    return out, truncated, external, hidden


def _no_server(spec: ServerSpec, refusals: list[str]) -> CodeIntelError:
    if refusals:
        return CodeIntelError(
            f"The {spec.id} language server was found but refused: {refusals[0]}",
            "server_refused",
            server=spec.id,
            refused=refusals,
        )
    return CodeIntelError(
        f"No {spec.id} language server is installed",
        "no_server",
        server=spec.id,
        install=install_hint(spec),
    )


def _location_answer(locations: list[tuple[str, tuple[int, int, int, int]]]) -> dict:
    """Locations as ``{path, line, character, endLine, endCharacter, preview}``.

    Lines are 1-based. A location outside the local filesystem (a ``jdt://``
    class file, a remote URI) or inside a credential store is dropped and
    counted in ``external`` / ``hidden``.
    """
    from kiro_crew.security import is_sensitive_path

    out: list[dict] = []
    external = hidden = 0
    truncated = False
    seen: set[tuple[str, int, int]] = set()
    lines_cache: dict[str, list[str] | None] = {}
    for uri, (sl, sc, el, ec) in locations:
        path = uri_to_path(uri)
        if path is None:
            external += 1
            continue
        if is_sensitive_path(path):
            hidden += 1
            continue
        if (path, sl, sc) in seen:
            continue
        seen.add((path, sl, sc))
        if len(out) >= MAX_LOCATIONS:
            truncated = True
            break
        out.append(
            {
                "path": path,
                "line": sl + 1,
                "character": sc,
                "endLine": el + 1,
                "endCharacter": ec,
                "preview": _preview(path, sl, lines_cache),
            }
        )
    out.sort(key=lambda item: (item["path"], item["line"], item["character"]))
    return {"locations": out, "truncated": truncated, "external": external, "hidden": hidden}


def _preview(path: str, line: int, cache: dict[str, list[str] | None]) -> str:
    if path not in cache:
        if len(cache) >= MAX_PREVIEW_FILES:
            return ""
        try:
            cache[path] = _read_text(path).splitlines()
        except CodeIntelError:
            cache[path] = None
    lines = cache[path]
    if lines is None or line >= len(lines):
        return ""
    return lines[line].strip()[:PREVIEW_CHARS]


_manager: LspManager | None = None
_manager_lock = threading.Lock()
_shutdown = False


def code_intel_manager() -> LspManager:
    """The process-wide pool, created on first use.

    Once :func:`shutdown_code_intel` has run, the pool stays shut: a request that
    lands during or after the gateway's cleanup is refused rather than given a
    fresh pool whose servers nothing would stop.
    """
    global _manager
    with _manager_lock:
        if _shutdown:
            raise CodeIntelError("Code intelligence is shutting down", "shutting_down")
        if _manager is None:
            _manager = LspManager()
        return _manager


def shutdown_code_intel() -> None:
    """Stop every running server and refuse new ones. Blocking; safe to call when none ever started."""
    global _manager, _shutdown
    with _manager_lock:
        _shutdown = True
        manager, _manager = _manager, None
    if manager is not None:
        manager.shutdown_all()


def stop_all_servers() -> None:
    """Stop every running server but leave the pool open for a later request.

    What the app's shutdown hook does, on disable and at gateway stop: the servers
    go, and enabling the app again gets a fresh pool. :func:`shutdown_code_intel`
    is the latching form, for a caller that must refuse requests during cleanup.
    """
    global _manager
    with _manager_lock:
        manager, _manager = _manager, None
    if manager is not None:
        manager.shutdown_all()
